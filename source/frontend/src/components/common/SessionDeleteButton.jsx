function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M9 3.75h6a1.25 1.25 0 0 1 1.25 1.25V6h3a.75.75 0 0 1 0 1.5h-1.04l-.6 10.18A2.5 2.5 0 0 1 15.11 20H8.89a2.5 2.5 0 0 1-2.5-2.32L5.79 7.5H4.75a.75.75 0 0 1 0-1.5h3V5A1.25 1.25 0 0 1 9 3.75Zm5.75 2.25V5.25H9.25V6h5.5Zm-6.86 1.5.5 9.99a1 1 0 0 0 1 .93h6.22a1 1 0 0 0 1-.93l.5-9.99H7.89Zm2.36 2.25c.41 0 .75.34.75.75v4.5a.75.75 0 0 1-1.5 0v-4.5c0-.41.34-.75.75-.75Zm3.5 0c.41 0 .75.34.75.75v4.5a.75.75 0 0 1-1.5 0v-4.5c0-.41.34-.75.75-.75Z"
        fill="currentColor"
      />
    </svg>
  );
}

export default function SessionDeleteButton({ title, disabled = false, onClick }) {
  return (
    <button
      type="button"
      className="session-delete-button"
      aria-label={`删除会话 ${title}`}
      title={`删除会话 ${title}`}
      disabled={disabled}
      onClick={onClick}
    >
      <TrashIcon />
    </button>
  );
}
