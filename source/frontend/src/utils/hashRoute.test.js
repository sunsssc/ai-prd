import assert from "node:assert/strict";
import test from "node:test";
import { buildAppHref, buildHash, parseHashRoute } from "./hashRoute.js";

test("构建 Skill 新建与导入独立路由", () => {
  assert.equal(buildHash("skill", { action: "new" }), "#/skill/new");
  assert.equal(buildHash("skill", { action: "new", mode: "manual" }), "#/skill/new?mode=manual");
  assert.equal(buildAppHref("skill", { action: "import" }), "/#/skill/import");
});

test("解析 Skill 新建与导入独立路由", () => {
  global.window = { location: { hash: "#/skill/new?mode=manual" } };
  assert.deepEqual(
    { page: parseHashRoute().page, action: parseHashRoute().skillAction, mode: parseHashRoute().skillMode },
    { page: "skill", action: "new", mode: "manual" }
  );

  global.window.location.hash = "#/skill/import";
  assert.deepEqual(
    { page: parseHashRoute().page, action: parseHashRoute().skillAction, mode: parseHashRoute().skillMode },
    { page: "skill", action: "import", mode: "" }
  );

  delete global.window;
});

test("保留需求评审入口动作参数", () => {
  assert.equal(
    buildHash("knowledge", {
      type: "requirements",
      nodeId: "workspace/requirements/tasks/example.md",
      reviewAction: "open"
    }),
    "#/knowledge?type=requirements&nodeId=workspace%2Frequirements%2Ftasks%2Fexample.md&reviewAction=open"
  );

  global.window = {
    location: {
      hash: "#/knowledge?type=requirements&nodeId=workspace%2Frequirements%2Ftasks%2Fexample.md&reviewAction=open"
    }
  };
  assert.equal(parseHashRoute().reviewAction, "open");
  delete global.window;
});
