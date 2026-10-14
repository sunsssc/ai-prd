import assert from "node:assert/strict";
import test from "node:test";
import {
  buildDirectoryChildCountMap,
  buildKnowledgeFullPath,
  buildKnowledgeSearchTree,
  getKnowledgeIndexScope,
  listKnowledgeDirectories,
  listKnowledgeFiles,
  matchKnowledgeFiles
} from "./knowledgeIndex.js";

const indexPayload = {
  prefixes: [
    "workspace/knowledge/requirements/",
    "workspace/knowledge/business-docs/",
    "workspace/knowledge/code/"
  ],
  directories: [
    [0, "docs/营销", 2],
    [0, "docs/营销/活动", 1],
    [1, "rules", 1]
  ],
  files: [
    [0, "docs/营销/优惠券规则.md"],
    [0, "docs/营销/活动/LaunchPlan.md"],
    [1, "rules/CouponPolicy.md"],
    [2, "coupon/service.py"]
  ]
};

const requirementsRoot = {
  id: "workspace/knowledge/requirements",
  path: "workspace/knowledge/requirements",
  kind: "folder",
  name: "requirements",
  children: [],
  child_count: 0
};

test("根据 rootPath 定位索引 scope", () => {
  assert.deepEqual(getKnowledgeIndexScope(indexPayload, "workspace/knowledge/requirements"), {
    prefix: "workspace/knowledge/requirements/",
    rootPath: "workspace/knowledge/requirements",
    scopeIndex: 0,
    scopeId: "requirements",
    scope: {
      id: "requirements",
      label: "需求文档",
      tone: "signal",
      rootPath: "workspace/knowledge/requirements",
      routeVisible: true
    }
  });
});

test("拼接完整知识库路径并列出标准化文件", () => {
  assert.equal(
    buildKnowledgeFullPath(indexPayload, 0, "/docs/营销/优惠券规则.md"),
    "workspace/knowledge/requirements/docs/营销/优惠券规则.md"
  );

  assert.deepEqual(listKnowledgeFiles(indexPayload, "business"), [
    {
      scopeIndex: 1,
      scopeId: "business",
      relativePath: "rules/CouponPolicy.md",
      fullPath: "workspace/knowledge/business-docs/rules/CouponPolicy.md",
      name: "CouponPolicy.md"
    }
  ]);
});

test("列出目录记录并保留 child_count", () => {
  assert.deepEqual(listKnowledgeDirectories(indexPayload, "requirements"), [
    {
      scopeIndex: 0,
      scopeId: "requirements",
      relativePath: "docs/营销",
      fullPath: "workspace/knowledge/requirements/docs/营销",
      name: "营销",
      childCount: 2
    },
    {
      scopeIndex: 0,
      scopeId: "requirements",
      relativePath: "docs/营销/活动",
      fullPath: "workspace/knowledge/requirements/docs/营销/活动",
      name: "活动",
      childCount: 1
    }
  ]);
});

test("文件候选使用统一归一化规则匹配大小写", () => {
  assert.deepEqual(
    matchKnowledgeFiles(indexPayload, "launch", { limit: 8 }).map((item) => item.fullPath),
    ["workspace/knowledge/requirements/docs/营销/活动/LaunchPlan.md"]
  );
});

test("目录 child_count 结合显式目录与文件索引", () => {
  const counts = buildDirectoryChildCountMap(requirementsRoot, indexPayload);

  assert.equal(counts.get("workspace/knowledge/requirements/docs/营销"), 2);
  assert.equal(counts.get("workspace/knowledge/requirements/docs/营销/活动"), 1);
});

test("搜索树包含命中文件及其父级目录", () => {
  const result = buildKnowledgeSearchTree(requirementsRoot, indexPayload, "优惠券");

  assert.equal(result.total, 1);
  assert.deepEqual([...result.expandedIds], [
    "workspace/knowledge/requirements",
    "workspace/knowledge/requirements/docs",
    "workspace/knowledge/requirements/docs/营销"
  ]);
  assert.equal(result.root.children[0].name, "docs");
  assert.equal(result.root.children[0].children[0].name, "营销");
  assert.equal(result.root.children[0].children[0].children[0].name, "优惠券规则.md");
});

test("组织知识库路径下仍可按文件名搜索", () => {
  const organizationIndexPayload = {
    prefixes: [
      "/coinex/knowledge/requirements/",
      "/coinex/knowledge/business-docs/",
      "/coinex/knowledge/code/"
    ],
    directories: [],
    files: [[0, "tasks/梳理中/【Admin】冻结用户列表排序优化__86eyagaev.md"]]
  };
  const organizationRequirementsRoot = {
    ...requirementsRoot,
    id: "/coinex/knowledge/requirements",
    path: "/coinex/knowledge/requirements"
  };

  const result = buildKnowledgeSearchTree(
    organizationRequirementsRoot,
    organizationIndexPayload,
    "【Admin】冻结用户列表排序优化"
  );

  assert.equal(result.total, 1);
  assert.equal(result.root.children[0].name, "tasks");
  assert.equal(result.root.children[0].children[0].name, "梳理中");
  assert.equal(result.root.children[0].children[0].children[0].name, "【Admin】冻结用户列表排序优化__86eyagaev.md");
});
