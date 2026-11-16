import { notFound } from "next/navigation";

import { Inbox } from "@/components/Inbox";
import { ApiError, getRun, getRunQuestions, listRuns } from "@/lib/api";

export const dynamic = "force-dynamic";

// The first paint comes from the server with the data already in it. After that the
// browser keeps it fresh.
export default async function RunPage(props: PageProps<"/runs/[id]">) {
  const { id } = await props.params;
  // A link from the Changes page names the question to open.
  const { question } = await props.searchParams;
  let run, questions, filename;
  try {
    [run, questions] = await Promise.all([getRun(id), getRunQuestions(id)]);
    filename = (await listRuns()).items.find((item) => item.id === id)?.filename ?? "Questionnaire";
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 422)) notFound();
    throw error;
  }
  // A public address the browser connects to directly. Read per request, so one image
  // serves any environment.
  const publicApi = process.env.PUBLIC_API_WS_URL ?? "ws://localhost:8000";
  return (
    <Inbox
      run={run}
      questions={questions}
      filename={filename}
      eventsUrl={`${publicApi}/runs/${id}/events`}
      initialQuestionId={typeof question === "string" ? question : null}
    />
  );
}
