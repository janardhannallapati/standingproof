"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

export function Providers({ children }: { children: ReactNode }) {
  // One cache per browser tab, created once. On the server, one per request.
  const [client] = useState(
    () => new QueryClient({ defaultOptions: { queries: { staleTime: 2000, retry: 1 } } }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
