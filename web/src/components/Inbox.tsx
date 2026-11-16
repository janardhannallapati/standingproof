"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  type Decision,
  type ReviewQuestion,
  type Run,
  type RunQuestion,
  approveReady,
  decide,
  getQuestion,
  getRun,
  getRunQuestions,
} from "@/lib/api";
import { GROUP_ORDER, allDecided, groupOf } from "@/lib/labels";
import { useRunEvents } from "@/lib/useRunEvents";

import { Button } from "./Button";
import type { Mode } from "./DecisionBar";
import { QuestionDetail } from "./QuestionDetail";
import { QuestionList } from "./QuestionList";
import { RunHeader } from "./RunHeader";

type Props = {
  run: Run;
  questions: RunQuestion[];
  filename: string;
  // Where the browser opens the live connection. A public address, never a secret.
  eventsUrl: string | null;
  maxRedrafts?: number;
  // The question to open first, when a link named one. Otherwise the first in the list.
  initialQuestionId?: string | null;
};

const POLL_MS = 5000;

// What is read out after a decision. The list moves on by itself, which is silent.
const SAID: Record<Decision, string> = {
  approve: "Approved.",
  edit: "Edited and approved.",
  reject: "Rejected.",
  redraft: "Sent back for a redraft.",
  keep: "Kept.",
};

/** The questions in the order the list shows them, so "next" means the next one down. */
function inListOrder(questions: RunQuestion[]): RunQuestion[] {
  return GROUP_ORDER.flatMap((group) => questions.filter((q) => groupOf(q) === group));
}

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

export function Inbox({
  run: firstRun,
  questions: firstQuestions,
  filename,
  eventsUrl,
  maxRedrafts = 2,
  initialQuestionId = null,
}: Props) {
  const client = useQueryClient();
  const runId = firstRun.id;
  const refresh = useCallback(() => {
    void client.invalidateQueries({ queryKey: ["run", runId] });
  }, [client, runId]);
  const live = useRunEvents(eventsUrl, refresh);
  // While the live connection is down, ask on a timer instead.
  const refetchInterval = live ? false : POLL_MS;

  const run = useQuery({
    queryKey: ["run", runId, "summary"],
    queryFn: () => getRun(runId),
    initialData: firstRun,
    refetchInterval,
  }).data;
  const questions = useQuery({
    queryKey: ["run", runId, "questions"],
    queryFn: () => getRunQuestions(runId),
    initialData: firstQuestions,
    refetchInterval,
  }).data;

  const ordered = useMemo(() => inListOrder(questions), [questions]);
  const linked = ordered.find((q) => q.id === initialQuestionId)?.id ?? null;
  const [selectedId, setSelectedId] = useState<string | null>(linked ?? ordered[0]?.id ?? null);
  // On a phone the list and the question are two screens; this says which is showing.
  // A link to one question opens that question.
  const [showDetail, setShowDetail] = useState(linked !== null);
  const [mode, setMode] = useState<Mode>("view");
  const [notice, setNotice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [said, setSaid] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  // Set by a decision; the next question to be shown takes the keyboard.
  const focusNext = useRef(false);

  const detail = useQuery({
    queryKey: ["run", runId, "question", selectedId],
    queryFn: () => getQuestion(selectedId as string),
    enabled: selectedId !== null,
  });

  const select = useCallback((id: string) => {
    setSelectedId(id);
    setShowDetail(true);
    setMode("view");
    setNotice(null);
  }, []);

  const step = useCallback(
    (by: number) => {
      const at = ordered.findIndex((q) => q.id === selectedId);
      const next = ordered[at + by];
      if (next) select(next.id);
    },
    [ordered, selectedId, select],
  );

  const decision = useMutation({
    mutationFn: (input: { item: ReviewQuestion; decision: Decision; answer?: string; comment?: string }) =>
      decide(input.item.id, {
        decision: input.decision,
        version: input.item.version,
        answer: input.answer ?? null,
        comment: input.comment ?? null,
      }),
    onSuccess: (updated, input) => {
      setSaid(SAID[input.decision]);
      focusNext.current = true;
      client.setQueryData(["run", runId, "question", updated.id], updated);
      refresh();
      setMode("view");
      if (updated.warnings.length > 0) {
        // Stay here: the reviewer should see what they just approved.
        setNotice(
          `Approved. Note: no cited source uses the word${updated.warnings.length === 1 ? "" : "s"} “${updated.warnings.join("”, “")}”.`,
        );
        return;
      }
      setNotice(null);
      // Move on to the next question still waiting, as the list orders them.
      const at = ordered.findIndex((q) => q.id === updated.id);
      const next = [...ordered.slice(at + 1), ...ordered.slice(0, at)].find((q) => q.review === "pending");
      if (next) setSelectedId(next.id);
    },
    onError: (error) => {
      setNotice(
        error instanceof ApiError && error.status === 409
          ? `Not saved: ${error.message}. The question has been reloaded.`
          : `Not saved: ${error instanceof Error ? error.message : "the request failed"}.`,
      );
      refresh();
    },
  });

  const readyCount = questions.filter((q) => groupOf(q) === "ready").length;
  const needsCount = questions.filter((q) => groupOf(q) === "needs").length;
  const batch = useMutation({
    mutationFn: () => approveReady(runId, readyCount),
    onSuccess: () => {
      setConfirming(false);
      refresh();
    },
    onError: (error) => {
      setConfirming(false);
      setNotice(`Nothing was approved: ${error instanceof Error ? error.message : "the request failed"}.`);
      refresh();
    },
  });

  const current = detail.data;
  // After a decision the bar that held the focus is gone. Put the keyboard on the heading
  // of whatever is shown now, so the next Tab starts from the question and not the page.
  useEffect(() => {
    if (focusNext.current && heading.current) {
      focusNext.current = false;
      heading.current.focus();
    }
  }, [current?.id, current?.version]);
  const onDecide = useCallback(
    (kind: Decision, extra?: { answer?: string; comment?: string }) => {
      if (current) decision.mutate({ item: current, decision: kind, ...extra });
    },
    [current, decision],
  );

  // One key per decision. Off while typing, and when a modifier is held (browser shortcuts).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "j") return step(1);
      if (key === "k") return step(-1);
      if (!current || current.review !== "pending" || mode !== "view" || decision.isPending) return;
      const hasDraft = Boolean(current.answer?.trim());
      if (key === "a") {
        // Not on a reopened question: there the choice is to keep, and that takes a click.
        if (!hasDraft || current.reopening) return;
        onDecide("approve");
      } else if (key === "e") setMode("edit");
      else if (key === "r") setMode("reject");
      else if (key === "d" && current.redrafts < maxRedrafts) setMode("redraft");
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, mode, decision.isPending, maxRedrafts, onDecide, step]);

  const readyAction =
    readyCount === 0 ? null : confirming ? (
      <span className="flex items-center gap-1.5">
        <Button primary className="!min-h-8" onClick={() => batch.mutate()} disabled={batch.isPending}>
          Approve {readyCount} unopened
        </Button>
        <Button className="!min-h-8" onClick={() => setConfirming(false)}>
          Cancel
        </Button>
      </span>
    ) : (
      <Button className="!min-h-8" onClick={() => setConfirming(true)}>
        Approve all {readyCount}
      </Button>
    );

  return (
    <div className="flex min-h-full flex-col">
      <RunHeader
        run={run}
        filename={filename}
        live={live}
        needs={needsCount}
        ready={readyCount}
        fileHref={allDecided(questions) ? `/runs/${run.id}/file` : null}
      />
      <p role="status" aria-label="Last decision" className="sr-only">
        {said}
      </p>
      <main className="grid flex-1 md:grid-cols-[400px_1fr]">
        <div className={`border-line md:block md:border-r ${showDetail ? "hidden" : ""}`}>
          <QuestionList
            questions={questions}
            selectedId={selectedId}
            onSelect={select}
            readyAction={readyAction}
          />
        </div>
        <div className={`min-w-0 md:block ${showDetail ? "" : "hidden"}`}>
          <div className="px-4 pt-2 md:hidden">
            <Button onClick={() => setShowDetail(false)}>‹ All questions</Button>
          </div>
          {current ? (
            <QuestionDetail
              // A different question, or a new version of this one, starts with a clean form.
              key={`${current.id}:${current.version}`}
              question={current}
              mode={mode}
              onMode={setMode}
              onDecide={onDecide}
              busy={decision.isPending}
              notice={notice}
              maxRedrafts={maxRedrafts}
              headingRef={heading}
            />
          ) : (
            <p className="p-6 text-muted" role="status">
              {questions.length === 0
                ? "This run has no questions."
                : detail.isError
                  ? "This question could not be loaded. It will be tried again."
                  : "Loading the question…"}
            </p>
          )}
        </div>
      </main>
    </div>
  );
}
