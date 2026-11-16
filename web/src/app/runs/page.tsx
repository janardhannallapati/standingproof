import Link from "next/link";

import { ApiError, listRuns } from "@/lib/api";

// Always fresh: this page is a view of work in progress.
export const dynamic = "force-dynamic";

function when(value: string): string {
  return new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

export default async function RunsPage() {
  let runs;
  try {
    runs = (await listRuns()).items;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "The API could not be reached.";
    return (
      <main className="mx-auto max-w-3xl p-6">
        <h1 className="text-xl font-semibold">Runs</h1>
        <p role="alert" className="mt-4 rounded-md bg-review-bg px-3 py-2.5 text-review">
          {message}
        </p>
      </main>
    );
  }
  return (
    <main className="mx-auto grid max-w-3xl gap-4 p-4 md:p-6">
      <h1 className="text-xl font-semibold">Runs</h1>
      {runs.length === 0 ? (
        <p className="text-muted">
          No questionnaire has been run yet. Upload one and start a run through the API; it will
          appear here.
        </p>
      ) : (
        <ul className="overflow-hidden rounded-md border border-line bg-surface">
          {runs.map((run) => (
            <li key={run.id} className="border-b border-line last:border-0">
              <Link href={`/runs/${run.id}`} className="flex min-h-11 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
                <span className="font-medium">{run.filename}</span>
                <span className="text-muted">{run.status}</span>
                <span className="text-[12.5px] text-muted tabular-nums md:ml-auto">{when(run.created_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
