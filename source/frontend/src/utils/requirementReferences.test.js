import assert from "node:assert/strict";
import test from "node:test";
import {
  buildRequirementReferenceMap,
  linkRequirementReferencesInTree,
  resolveRequirementReferenceHref
} from "./requirementReferences.js";

const indexPayload = {
  prefixes: [
    "workspace/knowledge/requirements/",
    "workspace/knowledge/business-docs/",
    "workspace/knowledge/code/"
  ],
  directories: [],
  files: [
    [0, "tasks/_deleted/旧名称__86exc9rmc.md"],
    [0, "tasks/历史迭代/【Admin】新用户任务风控熔断处置与恢复流程优化__86exc9rmc.md"],
    [0, "docs/说明__86ignored.md"],
    [1, "rules/业务规则.md"]
  ]
};

test("从需求任务文件名建立 ID、名称和文档链接映射", () => {
  const reference = buildRequirementReferenceMap(indexPayload).get("86exc9rmc");

  assert.deepEqual(reference, {
    id: "86exc9rmc",
    title: "【Admin】新用户任务风控熔断处置与恢复流程优化",
    path: "workspace/knowledge/requirements/tasks/历史迭代/【Admin】新用户任务风控熔断处置与恢复流程优化__86exc9rmc.md",
    href: "/#/knowledge?type=requirements&nodeId=workspace%2Fknowledge%2Frequirements%2Ftasks%2F%E5%8E%86%E5%8F%B2%E8%BF%AD%E4%BB%A3%2F%E3%80%90Admin%E3%80%91%E6%96%B0%E7%94%A8%E6%88%B7%E4%BB%BB%E5%8A%A1%E9%A3%8E%E6%8E%A7%E7%86%94%E6%96%AD%E5%A4%84%E7%BD%AE%E4%B8%8E%E6%81%A2%E5%A4%8D%E6%B5%81%E7%A8%8B%E4%BC%98%E5%8C%96__86exc9rmc.md"
  });
});

test("把助手正文中的需求 ID 替换为需求名称链接", () => {
  const tree = {
    type: "root",
    children: [
      {
        type: "paragraph",
        children: [{ type: "text", value: "规划中的优化（需求 #86exc9rmc 尚未上线）" }]
      }
    ]
  };

  linkRequirementReferencesInTree(tree, buildRequirementReferenceMap(indexPayload));

  assert.deepEqual(tree.children[0].children.map((node) => [node.type, node.value || node.children?.[0]?.value]), [
    ["text", "规划中的优化（需求 "],
    ["link", "【Admin】新用户任务风控熔断处置与恢复流程优化"],
    ["text", " 尚未上线）"]
  ]);
  assert.match(tree.children[0].children[1].url, /^\/#\/knowledge\?type=requirements&nodeId=/);
});

test("已有的纯 ID 链接会改为需求文档链接", () => {
  const tree = {
    type: "root",
    children: [
      { type: "link", url: "https://example.com", children: [{ type: "text", value: "#86exc9rmc" }] },
      { type: "inlineCode", value: "86unknown" }
    ]
  };

  linkRequirementReferencesInTree(tree, buildRequirementReferenceMap(indexPayload));

  assert.equal(tree.children[0].children[0].value, "【Admin】新用户任务风控熔断处置与恢复流程优化");
  assert.match(tree.children[0].url, /^\/#\/knowledge\?type=requirements&nodeId=/);
  assert.equal(tree.children[1].value, "86unknown");
});

test("表格中的行内代码 ID 会让同一回复里的需求名称成为链接", () => {
  const tree = {
    type: "root",
    children: [
      {
        type: "table",
        children: [
          {
            type: "tableRow",
            children: [
              {
                type: "tableCell",
                children: [
                  {
                    type: "text",
                    value: "【Admin】 新用户任务风控熔断处置与恢复流程优化"
                  }
                ]
              },
              {
                type: "tableCell",
                children: [{ type: "text", value: "Task ID " }, { type: "inlineCode", value: "86exc9rmc" }]
              }
            ]
          }
        ]
      }
    ]
  };

  linkRequirementReferencesInTree(tree, buildRequirementReferenceMap(indexPayload));

  const titleCellChildren = tree.children[0].children[0].children[0].children;
  assert.equal(titleCellChildren[0].type, "link");
  assert.equal(titleCellChildren[0].children[0].value, "【Admin】新用户任务风控熔断处置与恢复流程优化");
  assert.equal(tree.children[0].children[0].children[1].children[1].type, "inlineCode");
});

test("只有行内代码 ID 时直接渲染为需求名称链接", () => {
  const tree = {
    type: "root",
    children: [
      {
        type: "paragraph",
        children: [{ type: "text", value: "Task ID：" }, { type: "inlineCode", value: "86exc9rmc" }]
      }
    ]
  };

  linkRequirementReferencesInTree(tree, buildRequirementReferenceMap(indexPayload));

  assert.equal(tree.children[0].children[1].type, "link");
  assert.equal(tree.children[0].children[1].children[0].value, "【Admin】新用户任务风控熔断处置与恢复流程优化");
});

test("根据需求 ID 把 runtime 路径转换为索引中的站内文档链接", () => {
  const references = buildRequirementReferenceMap(indexPayload);
  assert.equal(
    resolveRequirementReferenceHref("coinex/knowledge/requirements/tasks/历史迭代/需求__86exc9rmc.md", references),
    references.get("86exc9rmc").href
  );
  assert.equal(resolveRequirementReferenceHref("https://example.com/requirements", references), "");
});
