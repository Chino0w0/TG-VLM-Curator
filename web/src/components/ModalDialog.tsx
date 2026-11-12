import { type ReactNode, useEffect, useId, useRef } from "react";

export interface ModalDialogProps {
  open: boolean;
  title: string;
  confirmLabel: string;
  destructive?: boolean;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children: ReactNode;
}

export function ModalDialog({
  open,
  title,
  confirmLabel,
  destructive = false,
  confirmDisabled = false,
  onConfirm,
  onCancel,
  children,
}: ModalDialogProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.hasAttribute("open")) {
      dialog.showModal();
    } else if (!open && dialog.hasAttribute("open")) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      onClose={() => {
        const dialog = dialogRef.current;
        if (!dialog) return;
        const action = dialog.returnValue;
        dialog.returnValue = "";
        if (action === "confirm") {
          onConfirm();
        } else {
          onCancel();
        }
      }}
    >
      <h2 id={titleId} style={{ margin: 0 }}>
        {title}
      </h2>
      <div>{children}</div>
      <form
        method="dialog"
        className="btn-row"
        style={{ justifyContent: "flex-end" }}
        onSubmit={(event) => {
          event.preventDefault();
          const submitter = (event.nativeEvent as SubmitEvent)
            .submitter as HTMLButtonElement | null;
          dialogRef.current?.close(submitter?.value ?? "");
        }}
      >
        <button
          type="submit"
          value="cancel"
          className="btn"
          autoFocus={destructive}
        >
          Cancel
        </button>
        <button
          type="submit"
          value="confirm"
          className={destructive ? "btn btn-danger" : "btn btn-primary"}
          disabled={confirmDisabled}
          autoFocus={!destructive}
        >
          {confirmLabel}
        </button>
      </form>
    </dialog>
  );
}
