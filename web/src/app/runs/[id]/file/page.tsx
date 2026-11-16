import Link from "next/link";
import { notFound } from "next/navigation";

import { FilePreview } from "@/components/FilePreview";
import { ApiError, getFilePreview } from "@/lib/api";

export const dynamic = "force-dynamic";

// The uploaded spreadsheet with the approved answers written into it: what will change,
// what stays empty and why, and the download. Read from the server on every visit, so it
// always shows the decisions as they are.
export default async function FilePage(props: PageProps<"/runs/[id]/file">) {
  const { id } = await props.params;
  let preview;
  try {
    preview = await getFilePreview(id);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 422)) notFound();
    throw error;
  }
  return (
    <>
      <header className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line bg-surface px-4 py-2.5">
        <span className="font-semibold">Standingproof</span>
        <span className="min-w-0 truncate text-muted">{preview.filename}</span>
        <Link
          href={`/runs/${id}`}
          className="inline-flex min-h-11 items-center rounded-md border border-line bg-surface px-3.5 font-medium md:ml-auto md:min-h-9"
        >
          ‹ All questions
        </Link>
      </header>
      <main aria-labelledby="file-title" className="mx-auto grid w-full max-w-215 gap-4 px-4 pt-5 pb-12">
        <div className="grid gap-1.5">
          <h1 id="file-title" className="text-[17px] leading-snug font-semibold">
            Filled file
          </h1>
          <p className="text-muted">
            Your spreadsheet with the approved answers written into it. Nothing else in the
            file is changed.
          </p>
        </div>
        <FilePreview runId={id} preview={preview} />
      </main>
    </>
  );
}
