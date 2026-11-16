"use client";

import { useEffect, useRef, useState } from "react";

const RETRY_MS = [1000, 2000, 5000, 10000];

/**
 * Keeps a live connection to one run and calls `onChanged` whenever the run changes.
 * Returns whether the connection is up. While it is down the caller should ask the API
 * on a timer instead: the message is only a hint, so nothing is lost by missing some.
 */
export function useRunEvents(url: string | null, onChanged: () => void): boolean {
  const [live, setLive] = useState(false);
  // The newest callback, without reconnecting every time the caller re-renders.
  const callback = useRef(onChanged);
  useEffect(() => {
    callback.current = onChanged;
  }, [onChanged]);

  useEffect(() => {
    if (!url) return;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      socket = new WebSocket(url);
      socket.onmessage = (event) => {
        let type: unknown;
        try {
          type = (JSON.parse(String(event.data)) as { type?: unknown }).type;
        } catch {
          return;
        }
        if (type === "ready") {
          attempt = 0;
          setLive(true);
          // Anything that changed while we were not listening is fetched now.
          callback.current();
        } else if (type === "changed") {
          callback.current();
        }
      };
      socket.onclose = () => {
        setLive(false);
        if (stopped) return;
        const wait = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
        attempt += 1;
        timer = setTimeout(connect, wait);
      };
    };
    connect();

    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, [url]);

  return live;
}
