// hooks/useEventStream.ts
// Connects to the backend WebSocket and dispatches typed events.
import { useEffect, useRef, useCallback, useState } from "react";

export type BusEvent = {
  topic: string;
  payload: Record<string, unknown>;
};

type EventHandler = (event: BusEvent) => void;

export function useEventStream(url: string) {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<BusEvent | null>(null);
  const handlersRef = useRef<Map<string, EventHandler[]>>(new Map());
  const wsRef = useRef<WebSocket | null>(null);

  const on = useCallback((topic: string, handler: EventHandler) => {
    const map = handlersRef.current;
    if (!map.has(topic)) map.set(topic, []);
    map.get(topic)!.push(handler);
    return () => {
      const arr = map.get(topic) || [];
      const idx = arr.indexOf(handler);
      if (idx > -1) arr.splice(idx, 1);
    };
  }, []);

  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let isMounted = true;

    function connect() {
      if (!isMounted) return;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (isMounted) setConnected(true);
      };

      ws.onmessage = (e) => {
        try {
          const event: BusEvent = JSON.parse(e.data);
          setLastEvent(event);
          const handlers = handlersRef.current.get(event.topic) || [];
          const wildcards = handlersRef.current.get("*") || [];
          [...handlers, ...wildcards].forEach((h) => h(event));
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        if (isMounted) {
          setConnected(false);
          reconnectTimer = setTimeout(connect, 3000);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    connect();

    return () => {
      isMounted = false;
      clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, [url]);

  return { connected, lastEvent, on };
}
