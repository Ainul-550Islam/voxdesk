"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDateTime, formatDuration, formatPhone, titleCase } from "@/lib/format";
import type { CallDetail, TranscriptTurn } from "@/lib/types";

export default function CallDetailPage() {
  const params = useParams<{ id: string }>();
  const callId = params.id;

  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [transcript, setTranscript] = useState<TranscriptTurn[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!callId || !getToken()) return;
    let cancelled = false;
    (async () => {
      try {
        const [detailRes, turns] = await Promise.all([
          api.callDetail(callId),
          api.transcript(callId),
        ]);
        if (cancelled) return;
        setDetail(detailRes);
        setTranscript(turns);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load call");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [callId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!detail) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>
          {formatPhone(detail.from)} → {formatPhone(detail.to)}
        </h1>
        <p className="muted">
          <span className={`badge ${detail.status}`}>{titleCase(detail.status)}</span>{" "}
          · {detail.direction} · {formatDateTime(detail.started_at)}
        </p>
      </header>

      <section className="card">
        <h2>Details</h2>
        <dl className="kv">
          <div>
            <dt>Duration</dt>
            <dd>{formatDuration(detail.duration)}</dd>
          </div>
          <div>
            <dt>Intent</dt>
            <dd>{detail.intent ?? "—"}</dd>
          </div>
          <div>
            <dt>Booked</dt>
            <dd>{detail.booked ? "Yes" : "No"}</dd>
          </div>
          <div>
            <dt>Escalated</dt>
            <dd>{detail.escalated ? "Yes" : "No"}</dd>
          </div>
          <div>
            <dt>Transfer state</dt>
            <dd>{titleCase(String(detail.transfer.state ?? "none"))}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>{detail.llm_used ?? "—"}</dd>
          </div>
          <div>
            <dt>Lead score</dt>
            <dd>{detail.lead_score ?? "—"}</dd>
          </div>
        </dl>
      </section>

      {detail.summary ? (
        <section className="card">
          <h2>Summary</h2>
          <p>{detail.summary}</p>
        </section>
      ) : null}

      <section className="card">
        <h2>Transcript</h2>
        {transcript.length === 0 ? (
          <div className="empty">No transcript turns recorded.</div>
        ) : (
          <div className="transcript">
            {transcript.map((turn, index) => (
              <div className={`turn ${turn.speaker}`} key={index}>
                <div className="turn-head">
                  <span className="turn-speaker">{turn.speaker}</span>
                  <span className="turn-at">{formatDateTime(turn.at)}</span>
                </div>
                <p className="turn-text">{turn.text}</p>
              </div>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
