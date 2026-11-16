import { RunsList } from "@/components/RunsList";
import { Sections } from "@/components/Sections";
import { ApiError, listRuns } from "@/lib/api";

// Always fresh: this page is a view of work in progress.
export const dynamic = "force-dynamic";

export default async function RunsPage() {
  let runs;
  try {
    runs = (await listRuns()).items;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "The API could not be reached.";
    return (
      <main className="mx-auto grid max-w-3xl gap-4 p-4 md:p-6">
        <Sections current="/runs" />
        <p role="alert" className="rounded-md bg-review-bg px-3 py-2.5 text-review">
          {message}
        </p>
      </main>
    );
  }
  return (
    <main className="mx-auto grid max-w-3xl gap-4 p-4 md:p-6">
      <Sections current="/runs" />
      <h1 className="sr-only">Runs</h1>
      {runs.length === 0 ? (
        <p className="text-muted">
          No questionnaire has been run yet. Upload one and start a run through the API; it will
          appear here.
        </p>
      ) : (
        <RunsList runs={runs} />
      )}
    </main>
  );
}
