"use client";

import Link from "next/link";
import { useRef, useState } from "react";

import { ApiError, type FilePreview as Preview, fileUrl, makeFile } from "@/lib/api";
import { emptyReason, utcDay } from "@/lib/labels";

import { Button } from "./Button";
import { SentBox } from "./SentBox";
import { StatusChip } from "./StatusChip";

type Props = {
  runId: string;
  preview: Preview;
  // How the browser is sent to the file. Replaced in tests.
  navigate?: (url: string) => void;
};

const HOW: Record<string, string> = { approved: "approved", edited: "edited by a reviewer" };

/** Where a written cell's text came from, in words. */
function origin(cell: { how: string; kind: string }): string {
  if (cell.kind === "choice") {
    return "the dropdown's value, taken from the first word of the approved answer";
  }
  const how = HOW[cell.how] ?? cell.how;
  return cell.kind === "comment" ? `${how} · the full answer, in the comments column` : how;
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function toFile(url: string) {
  window.location.assign(url);
}

export function FilePreview({ runId, preview, navigate = toFile }: Props) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // The file for the decisions as they are: one made earlier, or the one just made.
  const [fileId, setFileId] = useState(preview.latest?.current ? preview.latest.id : null);
  // True from the click until the reply. State alone is too late for a double click.
  const making = useRef(false);

  async function download() {
    if (making.current) return;
    making.current = true;
    setBusy(true);
    setProblem(null);
    try {
      // Asking again for the same decisions returns the same file, so a second click or
      // a retry after a lost reply is safe.
      const file = await makeFile(runId);
      setFileId(file.id);
      navigate(fileUrl(runId, file.id));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "The file could not be made. Try again.");
    } finally {
      making.current = false;
      setBusy(false);
    }
  }

  if (!preview.ready) {
    const reopened = preview.sent.some((record) => record.reopened > 0);
    return (
      <div className="grid gap-3">
        <p className="flex items-start gap-2.5 rounded-md bg-review-bg px-3 py-2.5 font-medium text-review">
          <span aria-hidden="true">!</span>
          <span>
            {count(preview.undecided, "question still needs", "questions still need")} a decision.
          </span>
        </p>
        <p className="text-muted">
          The file is made when every question has one.
          {reopened ? " An answer was reopened because a live setting changed." : ""}
        </p>
        <p>
          <Link
            href={`/runs/${runId}`}
            className="inline-flex min-h-11 items-center font-medium text-accent underline"
          >
            Back to the questions
          </Link>
        </p>
        {preview.sent.length > 0 ? (
          // Who has a file made from the decisions as they were. The records themselves
          // are never changed; "out of date" is worked out from them.
          <div className="grid gap-2 rounded-md border border-line bg-surface p-3">
            <h2 id="sent-before" className="text-[12.5px] font-semibold text-muted">
              Sent before this change
            </h2>
            <ul aria-labelledby="sent-before" className="grid gap-1.5">
              {preview.sent.map((record) => (
                <li key={record.id} className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                  {record.current && record.reopened === 0 ? (
                    <StatusChip tone="ready" mark="✓" label="Sent" />
                  ) : (
                    <StatusChip tone="review" mark="!" label="Out of date" />
                  )}
                  <span>
                    to {record.customer} on {utcDay(record.sent_at)} by {record.sent_by} ·{" "}
                    {count(record.answers, "answer", "answers")}
                    {record.reopened > 0 ? `, ${record.reopened} reopened since` : ""}
                  </span>
                </li>
              ))}
            </ul>
            <p className="text-[12.5px] text-muted">
              These customers have the earlier wording. When every question is decided,
              download the file again, send it, and record it here.
            </p>
          </div>
        ) : null}
      </div>
    );
  }

  const written = preview.written.length;
  const empty = preview.left_empty.length;
  // One answer can fill two cells: a dropdown's value and the sentence beside it.
  const answers = new Set(preview.written.map((cell) => cell.position)).size;
  const cells = written === answers ? "" : `, in ${count(written, "cell", "cells")}`;
  // Questions whose sentence went into the comments column.
  const commented = new Set(
    preview.written.filter((cell) => cell.kind === "comment").map((cell) => cell.position),
  );
  return (
    <div className="grid min-w-0 gap-4">
      {written === 0 ? (
        <p className="flex items-start gap-2.5 rounded-md bg-review-bg px-3 py-2.5 font-medium text-review">
          <span aria-hidden="true">!</span>
          <span>
            No answer will be written. {count(empty, "cell is", "cells are")} left empty, for the
            reasons below. The file you download would be your upload, unchanged.
          </span>
        </p>
      ) : (
        <p className="flex items-start gap-2.5 rounded-md bg-ready-bg px-3 py-2.5 font-medium text-ready">
          <span aria-hidden="true">✓</span>
          <span>
            {count(answers, "answer", "answers")} will be written{cells}.{" "}
            {count(empty, "cell is", "cells are")} left empty. The result is compared with your
            upload before you get it: if anything else in the file differs, no file is made.
          </span>
        </p>
      )}

      {preview.protected_sheets.map((sheet) => (
        <p key={sheet} className="text-[12.5px] text-muted">
          The sheet {sheet} is protected. The answers are written into the file all the same.
        </p>
      ))}
      {preview.latest && !preview.latest.current ? (
        <p className="text-[12.5px] text-muted">
          A file was made earlier, before a decision changed. Downloading now makes a new one.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {/* aria-disabled, not disabled: a disabled button loses the keyboard's focus. */}
        <Button primary onClick={download} aria-disabled={busy} className={busy ? "opacity-50" : ""}>
          {busy ? "Making the file…" : "Download filled file"}
        </Button>
        <span className="text-[12.5px] text-muted">{preview.filename}</span>
      </div>
      {problem ? (
        <p role="alert" className="rounded-md bg-review-bg px-3 py-2.5 font-medium text-review">
          {problem}
        </p>
      ) : null}

      {/* Keyed by the file: another file starts with its own records. */}
      {fileId ? <SentBox key={fileId} runId={runId} fileId={fileId} records={preview.sent} /> : null}

      <div className="grid gap-1.5">
        <h2 id="written" className="text-[12.5px] font-semibold text-muted">
          What is written ({written})
        </h2>
        {written === 0 ? (
          <p className="rounded-md border border-line bg-surface px-3 py-2.5 text-muted">
            No answer was approved, so no cell changes.
          </p>
        ) : (
          <div
            // On a narrow screen this box scrolls sideways, so a keyboard must reach it.
            role="region"
            aria-labelledby="written"
            tabIndex={0}
            className="overflow-x-auto rounded-md border border-line bg-surface"
          >
            <table aria-label="What is written" className="w-full min-w-130 border-collapse">
              <thead>
                <tr className="text-left text-[12.5px] text-muted">
                  <th scope="col" className="px-3 py-2 font-semibold">Cell</th>
                  <th scope="col" className="px-3 py-2 font-semibold">Was</th>
                  <th scope="col" className="px-3 py-2 font-semibold">Will be</th>
                </tr>
              </thead>
              <tbody>
                {preview.written.map((cell) => (
                  <tr key={`${cell.sheet}!${cell.cell}`} className="border-t border-line align-top">
                    <th scope="row" className="px-3 py-2.5 text-left font-semibold whitespace-nowrap tabular-nums">
                      {cell.sheet}!{cell.cell}
                    </th>
                    <td className="px-3 py-2.5 whitespace-nowrap text-muted">empty</td>
                    <td className="px-3 py-2.5">
                      <div className="text-[12.5px] text-muted">{cell.question}</div>
                      <div className="mt-1 whitespace-pre-wrap">{cell.wording}</div>
                      <div className="mt-1.5 text-[12.5px] text-muted">{origin(cell)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {empty > 0 ? (
        <div className="grid gap-1.5">
          <h2 className="text-[12.5px] font-semibold text-muted">Left empty ({empty})</h2>
          <ul aria-label="Left empty" className="grid gap-2">
            {preview.left_empty.map((cell) => {
              const reason = emptyReason(cell.reason, cell.detail);
              return (
                <li
                  key={`${cell.sheet}!${cell.cell ?? cell.position}:${cell.reason}`}
                  className="grid gap-1.5 rounded-md border border-line bg-surface px-3 py-2.5"
                >
                  <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                    <span className="font-semibold tabular-nums">
                      {cell.cell ? `${cell.sheet}!${cell.cell}` : cell.sheet}
                    </span>
                    <StatusChip tone={reason.tone} mark={reason.mark} label={reason.label} />
                  </div>
                  <div>{cell.question}</div>
                  <div className="text-[12.5px] text-muted">
                    {reason.text}
                    {cell.reason === "dropdown" && commented.has(cell.position)
                      ? " The full answer is in the comments column beside it. Choose the value in the spreadsheet."
                      : ""}
                    {cell.comment ? ` The reviewer's note: “${cell.comment}”` : ""}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
