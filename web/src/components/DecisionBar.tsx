"use client";

import { useId, useState } from "react";

import type { Decision, ReviewQuestion } from "@/lib/api";

import { Button } from "./Button";

export type Mode = "view" | "edit" | "reject" | "redraft";

type Props = {
  question: ReviewQuestion;
  mode: Mode;
  onMode: (mode: Mode) => void;
  onDecide: (decision: Decision, extra?: { answer?: string; comment?: string }) => void;
  busy: boolean;
  maxRedrafts: number;
};

type Prompt = { label: string; submit: string; hint: string };

// Rejecting an answer that customers already have is withdrawing it.
const WITHDRAW: Prompt = {
  label: "Why is this withdrawn? (optional)",
  submit: "Withdraw",
  hint: "No answer will be given for this question: its cell is left empty in the next file.",
};

const PROMPTS: Record<Exclude<Mode, "view">, Prompt> = {
  edit: {
    label: "Answer to approve",
    submit: "Save and approve",
    hint: "Your wording becomes the final answer. The draft is kept beside it.",
  },
  reject: {
    label: "Why is this rejected? (optional)",
    submit: "Reject",
    hint: "No answer will be given for this question.",
  },
  redraft: {
    label: "What should the new draft do differently?",
    submit: "Send back",
    hint: "The agent drafts again with this instruction, and the new draft returns here.",
  },
};

/** The four decisions. Edit, reject and redraft each open one text field first. */
export function DecisionBar({ question, mode, onMode, onDecide, busy, maxRedrafts }: Props) {
  const [text, setText] = useState("");
  const fieldId = useId();
  const hasDraft = Boolean(question.answer?.trim());
  // Waiting again because a live setting changed. What is on the table is then the wording
  // a person approved, where there is one, and not the agent's draft.
  const reopened = Boolean(question.reopening);
  const standing = (reopened ? question.final_answer : null) ?? question.answer ?? "";
  // "Withdraw" only for an answer a person had approved. A draft nobody decided is rejected.
  const withdrawing = Boolean(question.reopening?.was_review);
  const redraftsLeft = maxRedrafts - question.redrafts;

  const open = (next: Exclude<Mode, "view">) => {
    setText(next === "edit" ? standing : "");
    onMode(next);
  };

  if (mode !== "view") {
    const prompt = withdrawing && mode === "reject" ? WITHDRAW : PROMPTS[mode];
    const required = mode !== "reject";
    const submit = () => {
      if (mode === "edit") onDecide("edit", { answer: text });
      else onDecide(mode, { comment: text });
    };
    return (
      <form
        className="grid gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label htmlFor={fieldId} className="text-[12.5px] font-semibold text-muted">
          {prompt.label}
        </label>
        <textarea
          id={fieldId}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") onMode("view");
          }}
          rows={mode === "edit" ? 5 : 3}
          maxLength={4000}
          autoFocus
          className="w-full rounded-md border border-line bg-surface px-3.5 py-3 text-[15px] leading-normal"
        />
        <p className="text-[12.5px] text-muted">{prompt.hint}</p>
        <div className="grid grid-cols-2 gap-2 md:flex">
          <Button primary type="submit" disabled={busy || (required && !text.trim())}>
            {prompt.submit}
          </Button>
          <Button onClick={() => onMode("view")} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    );
  }

  if (reopened) {
    return (
      <div className="grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-center">
        <Button primary shortcut="E" onClick={() => open("edit")} disabled={busy}>
          Edit
        </Button>
        {standing.trim() ? (
          // No key for this one: keeping an answer that a changed setting may contradict
          // takes a deliberate click.
          // aria-disabled, not disabled: a disabled button drops the keyboard's focus.
          <Button
            onClick={() => {
              if (!busy) onDecide("keep");
            }}
            aria-disabled={busy}
            className={busy ? "opacity-50" : ""}
          >
            Keep this answer
          </Button>
        ) : null}
        <Button shortcut="R" onClick={() => open("reject")} disabled={busy}>
          {withdrawing ? "Withdraw" : "Reject"}
        </Button>
        <Button
          shortcut="D"
          onClick={() => open("redraft")}
          disabled={busy || redraftsLeft <= 0}
          title={redraftsLeft <= 0 ? `Already redrafted ${maxRedrafts} times` : undefined}
        >
          Ask for a redraft
        </Button>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-center">
      {hasDraft ? (
        <Button primary shortcut="A" onClick={() => onDecide("approve")} disabled={busy}>
          Approve
        </Button>
      ) : (
        <Button primary shortcut="E" onClick={() => open("edit")} disabled={busy}>
          Write an answer
        </Button>
      )}
      {hasDraft ? (
        <Button shortcut="E" onClick={() => open("edit")} disabled={busy}>
          Edit
        </Button>
      ) : null}
      <Button shortcut="R" onClick={() => open("reject")} disabled={busy}>
        Reject
      </Button>
      <Button
        shortcut="D"
        onClick={() => open("redraft")}
        disabled={busy || redraftsLeft <= 0}
        title={redraftsLeft <= 0 ? `Already redrafted ${maxRedrafts} times` : undefined}
      >
        Ask for a redraft
      </Button>
    </div>
  );
}
