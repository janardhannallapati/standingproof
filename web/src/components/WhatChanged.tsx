import type { Reopening } from "@/lib/api";
import { factLabel, factValue, foundBy, settingName, utcDay } from "@/lib/labels";

/**
 * The live settings a reopened answer rests on: what each read when the answer was
 * approved, and what it reads now. Every statement here was built by the API from a
 * check's named facts; it is rendered as text.
 */
export function WhatChanged({ reopening }: { reopening: Reopening }) {
  const then = reopening.was_review ? "When approved" : "When drafted";
  return (
    <div className="grid gap-1.5">
      <h2 className="text-[12.5px] font-semibold text-muted">What changed</h2>
      {reopening.settings.map((setting) => {
        const name = settingName(setting.check_name, setting.scope);
        return (
          <div key={name} className="overflow-hidden rounded-md border border-line bg-surface">
            <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-[12.5px] text-muted">
              <b className="font-semibold text-fg">Live check</b>
              <span className="min-w-0 break-words">{name}</span>
            </p>
            {setting.changes.length === 0 ? (
              <p className="border-b border-line px-3 py-2.5">
                This setting changed and has changed back. It reads now as it did when the answer
                was {reopening.was_review ? "approved" : "drafted"}.
              </p>
            ) : (
              // A long value wraps; if the table is still wider than a phone it scrolls
              // inside this box, which a keyboard can reach. Nothing is cut off.
              <div role="region" aria-label={`${name}, what changed`} tabIndex={0} className="overflow-x-auto">
              <table aria-label={name} className="w-full border-collapse">
                <thead>
                  <tr className="text-left align-bottom text-[12.5px] text-muted">
                    <th scope="col" className="px-3 py-2 font-semibold">Setting</th>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      {then}
                      {setting.was ? <span className="block font-normal">seen {utcDay(setting.was.observed_at)}</span> : null}
                    </th>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      Now
                      <span className="block font-normal">seen {utcDay(setting.now.observed_at)}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {setting.changes.map((changed) => (
                    <tr key={changed.fact} className="border-t border-line align-top">
                      <th scope="row" className="px-3 py-2 text-left font-normal">{factLabel(changed.fact)}</th>
                      <td className="px-3 py-2 break-words text-muted tabular-nums">{factValue(changed.was)}</td>
                      <td className="px-3 py-2 tabular-nums">
                        <span className="rounded bg-review-bg box-decoration-clone px-1.5 py-0.5 font-semibold break-words text-review">
                          <span aria-hidden="true">! </span>
                          <span className="sr-only">changed: </span>
                          {factValue(changed.now)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
            <p className="border-t border-line px-3 pt-2.5">{setting.now.statement}</p>
            <p className="px-3 pt-1.5 pb-2.5 text-[12.5px] text-muted">{foundBy(setting.trigger)}</p>
          </div>
        );
      })}
    </div>
  );
}
