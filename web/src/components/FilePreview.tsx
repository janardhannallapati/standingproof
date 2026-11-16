"use client";

import Link from "next/link";
import { useState } from "react";

import { ApiError, type FilePreview as Preview, fileUrl, makeFile } from "@/lib/api";
import { emptyReason } from "@/lib/labels";

import { Button } from "./Button";
import { StatusChip } from "./StatusChip";

type Props = {
  runId: string;
  preview: Preview;
  // How the browser is sent to the file. Replaced in tests.
  navigate?: (url: string) => void;
};

const HOW: Record<string, string> = { approved: "approved", edited: "edited by a reviewer" };

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function toFile(url: string) {
  window.location.assign(url);
}

export function FilePreview({ runId, preview, navigate = toFile }: Props) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setProblem(null);
    try {
      // Asking again for the same decisions returns the same file, so a second click or
      // a retry after a lost reply is safe.
      const file = await makeFile(runId);
      navigate(fileUrl(runId, file.id));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "The file could not be made. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!preview.ready) {
    return (
      <div className="grid gap-3">
        <p className="flex items-start gap-2.5 rounded-md bg-review-bg px-3 py-2.5 font-medium text-review">
          <span aria-hidden="true">!</span>
          <span>
            {count(preview.undecided, "question still needs", "questions still need")} a decision.
          </span>
        </p>
        <p className="text-muted">The file is made when every question has one.</p>
        <p>
          <Link href={`/runs/${runId}`} className="font-medium text-accent underline">
            Back to the questions
          </Link>
        </p>
      </div>
    );
  }

  const written = preview.written.length;
  const empty = preview.left_empty.length;
  return (
    <div className="grid min-w-0 gap-4">
      <p className="flex items-start gap-2.5 rounded-md bg-ready-bg px-3 py-2.5 font-medium text-ready">
        <span aria-hidden="true">✓</span>
        <span>
          {count(written, "answer", "answers")} will be written. {count(empty, "cell is", "cells are")} left
          empty. The result is compared with your upload before you get it: if anything else
          in the file differs, no file is made.
        </span>
      </p>

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
        <Button primary onClick={download} disabled={busy}>
          {busy ? "Making the file…" : "Download filled file"}
        </Button>
        <span className="text-[12.5px] text-muted">{preview.filename}</span>
      </div>
      {problem ? (
        <p role="alert" className="rounded-md bg-review-bg px-3 py-2.5 font-medium text-review">
          {problem}
        </p>
      ) : null}

      <div className="grid gap-1.5">
        <h2 id="written" className="text-[12.5px] font-semibold text-muted">
          What is written ({written})
        </h2>
        {written === 0 ? (
          <p className="rounded-md border border-line bg-surface px-3 py-2.5 text-muted">
            No answer was approved, so no cell changes.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-line bg-surface">
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
                  <tr key={cell.position} className="border-t border-line align-top">
                    <th scope="row" className="px-3 py-2.5 text-left font-semibold whitespace-nowrap tabular-nums">
                      {cell.sheet}!{cell.cell}
                    </th>
                    <td className="px-3 py-2.5 whitespace-nowrap text-muted">empty</td>
                    <td className="px-3 py-2.5">
                      <div className="text-[12.5px] text-muted">{cell.question}</div>
                      <div className="mt-1 whitespace-pre-wrap">{cell.wording}</div>
                      <div className="mt-1.5 text-[12.5px] text-muted">{HOW[cell.how] ?? cell.how}</div>
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
                <li key={cell.position} className="grid gap-1.5 rounded-md border border-line bg-surface px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                    <span className="font-semibold tabular-nums">
                      {cell.cell ? `${cell.sheet}!${cell.cell}` : cell.sheet}
                    </span>
                    <StatusChip tone={reason.tone} mark={reason.mark} label={reason.label} />
                  </div>
                  <div>{cell.question}</div>
                  <div className="text-[12.5px] text-muted">
                    {reason.text}
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
