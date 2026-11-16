import { ChangesList } from "@/components/ChangesList";
import { Sections } from "@/components/Sections";
import { ApiError, listChanges } from "@/lib/api";

// Always fresh: a change may have been seen a moment ago.
export const dynamic = "force-dynamic";

export default async function ChangesPage() {
  let changes;
  try {
    changes = (await listChanges()).items;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "The API could not be reached.";
    return (
      <main className="mx-auto grid max-w-3xl gap-4 p-4 md:p-6">
        <Sections current="/changes" />
        <h1 className="sr-only">Changes</h1>
        <p role="alert" className="rounded-md bg-review-bg px-3 py-2.5 text-review">
          {message}
        </p>
      </main>
    );
  }
  return (
    <main className="mx-auto grid max-w-3xl gap-4 p-4 md:p-6">
      <Sections current="/changes" />
      <h1 className="sr-only">Changes</h1>
      <p className="text-muted">
        Live settings that have changed since an answer resting on them was approved.
      </p>
      <ChangesList changes={changes} />
    </main>
  );
}
