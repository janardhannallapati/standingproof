"use client";

import { type FormEvent, useId, useRef, useState } from "react";

import { ApiError, type SentRecord, recordSent } from "@/lib/api";

import { Button } from "./Button";
import { StatusChip } from "./StatusChip";

type Props = { runId: string; fileId: string; records: SentRecord[] };

function day(iso: string): string {
  // The same text on the server, in the browser and in tests, whatever the time zone.
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** Where a reviewer records that the file went to a customer, and what was recorded. */
export function SentBox({ runId, fileId, records: first }: Props) {
  const field = useId();
  const [records, setRecords] = useState(first);
  const [customer, setCustomer] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // Read out when a record is made: the new line in the list is otherwise silent.
  const [said, setSaid] = useState("");
  const input = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const name = customer.trim();
    if (!name) {
      setProblem("Say who the file was sent to.");
      setSaid("");
      // Back to the field that is the problem, not left on the button.
      input.current?.focus();
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      const record = await recordSent(runId, fileId, name);
      // Recording the same customer twice returns the first record: show it once.
      setRecords((had) => (had.some((each) => each.id === record.id) ? had : [...had, record]));
      setCustomer("");
      setSaid(`Recorded as sent to ${record.customer}.`);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "That was not recorded. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-2 rounded-md border border-line bg-surface p-3">
      <h2 className="text-[12.5px] font-semibold text-muted">After you send it</h2>
      {records.length > 0 ? (
        <ul aria-label="Recorded as sent" className="grid gap-1.5">
          {records.map((record) => (
            <li key={record.id} className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              <StatusChip tone="ready" mark="✓" label="Sent" />
              <span>
                to {record.customer} on {day(record.sent_at)} by {record.sent_by} ·{" "}
                {record.answers} {record.answers === 1 ? "answer" : "answers"}
              </span>
              {record.current ? null : (
                <span className="text-[12.5px] text-muted">(a file made before a decision changed)</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      <form onSubmit={submit} noValidate className="flex flex-wrap items-end gap-x-3 gap-y-2">
        <div className="grid gap-1">
          <label htmlFor={field} className="text-[12.5px] font-semibold text-muted">
            Sent to
          </label>
          <input
            id={field}
            ref={input}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? `${field}-problem` : undefined}
            value={customer}
            onChange={(event) => setCustomer(event.target.value)}
            maxLength={200}
            autoComplete="off"
            className="min-h-11 w-60 max-w-full rounded-md border border-line bg-surface px-2.5 md:min-h-9"
          />
        </div>
        <Button type="submit" aria-disabled={busy} className={busy ? "opacity-50" : ""}>
          Record as sent
        </Button>
      </form>
      {problem ? (
        <p
          id={`${field}-problem`}
          role="alert"
          className="rounded-md bg-review-bg px-3 py-2.5 font-medium text-review"
        >
          {problem}
        </p>
      ) : null}
      <p role="status" className="sr-only">
        {said}
      </p>
      <p className="text-[12.5px] text-muted">
        Do this after you have sent the file. It records that you said so, with the wording of
        each answer as it went out.
      </p>
    </div>
  );
}
