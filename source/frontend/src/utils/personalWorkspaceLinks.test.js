import assert from "node:assert/strict";
import test from "node:test";
import {
  buildPersonalWorkspaceFileUrl,
  normalizePersonalWorkspaceFilePath,
  resolvePersonalWorkspaceLinkHref
} from "./personalWorkspaceLinks.js";

test("构建个人 workspace 文件下载地址", () => {
  assert.equal(
    buildPersonalWorkspaceFileUrl("/me/files/report.md"),
    "/api/workspace/me/file?path=me%2Ffiles%2Freport.md"
  );
  assert.equal(
    buildPersonalWorkspaceFileUrl("sandbox:/me/artifacts/demo.html"),
    "/api/workspace/me/file?path=me%2Fartifacts%2Fdemo.html"
  );
  assert.equal(
    buildPersonalWorkspaceFileUrl("/me/files/report%20final.md"),
    "/api/workspace/me/file?path=me%2Ffiles%2Freport+final.md"
  );
  assert.equal(
    buildPersonalWorkspaceFileUrl("workspace/users/user-1/files/report.pdf"),
    "/api/workspace/me/file?path=workspace%2Fusers%2Fuser-1%2Ffiles%2Freport.pdf"
  );
});

test("个人 workspace 链接解析拒绝非个人路径和越界路径", () => {
  assert.equal(normalizePersonalWorkspaceFilePath("https://example.com/report.md"), "");
  assert.equal(normalizePersonalWorkspaceFilePath("/coinex/knowledge/requirements/a.md"), "");
  assert.equal(normalizePersonalWorkspaceFilePath("/me/../secret.md"), "");
  assert.equal(resolvePersonalWorkspaceLinkHref("https://example.com/report.md"), "https://example.com/report.md");
});
