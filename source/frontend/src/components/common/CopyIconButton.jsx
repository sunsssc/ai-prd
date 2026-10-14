import { useEffect, useState } from "react";
import ActionIcon from "./ActionIcon";

export default function CopyIconButton({
  value = "",
  idleLabel = "复制",
  successLabel = "已复制",
  errorLabel = "复制失败",
  className = "",
  buttonClassName = "message-action-button",
  successClassName = "message-action-button-success",
  errorClassName = "message-action-button-error"
}) {
  const [copyState, setCopyState] = useState("idle");
  const canCopy = Boolean(value);

  useEffect(() => {
    if (copyState === "idle" || copyState === "copying") {
      return undefined;
    }

    const timerId = window.setTimeout(() => {
      setCopyState("idle");
    }, 1800);

    return () => {
      window.clearTimeout(timerId);
    };
  }, [copyState]);

  async function handleCopy() {
    if (!canCopy || copyState === "copying") {
      return;
    }

    setCopyState("copying");

    try {
      await navigator.clipboard.writeText(value);
      setCopyState("success");
    } catch {
      setCopyState("error");
    }
  }

  const label =
    copyState === "success"
      ? successLabel
      : copyState === "error"
        ? errorLabel
        : copyState === "copying"
          ? "正在复制"
          : idleLabel;

  const stateClassName =
    copyState === "success"
      ? successClassName
      : copyState === "error"
        ? errorClassName
        : "";

  return (
    <button
      type="button"
      className={[buttonClassName, stateClassName, className].filter(Boolean).join(" ")}
      aria-label={label}
      title={label}
      disabled={!canCopy || copyState === "copying"}
      onClick={handleCopy}
    >
      <ActionIcon kind={copyState === "success" ? "check" : "copy"} />
    </button>
  );
}
