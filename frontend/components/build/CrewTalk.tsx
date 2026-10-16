"use client";

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api, type CrewMessage, type CrewReply, type Project } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import { ago, useNow } from "@/components/setup/parts";
import { listOf } from "@/lib/text";
import { Composer } from "./Changes";

/**
 * Talk to the crew while it builds (#102).
 *
 * The build page's one box, in the slot that holds whatever needs you: the same
 * composer as "What should change?", with the conversation above it. A question is
 * answered from the build's own record at once; a note waits for the agents it's for.
 * On a stuck build the box is the stuck card's "Tell the crew what to try", and a note
 * sent there starts the next fix round with it.
 */

type Act = (fn: () => Promise<unknown>) => Promise<boolean>;

/** Where the box sits, which decides its words. */
export type TalkPlace = "running" | "stuck" | "stopped" | "gate";

const COPY: Record<TalkPlace, { label: string; placeholder: string; quick: string[]; hint: string }> = {
  running: {
    label: "Talk to the crew",
    placeholder: "Ask what they're doing, or tell them what you want",
    quick: ["What are you doing?", "Who's next?"],
    hint: "The crew answers questions straight away. A note goes to every agent still to work, or to the one you name first (\u201cFrontend: use a grid\u201d). If it changes work already done, that work changes after the step in hand.",
  },
  stuck: {
    label: "Tell the crew what to try",
    placeholder: "Keep all the routes in one file, for example, or ask why it's stuck",
    quick: ["Why is it stuck?"],
    hint: "A note starts the next fix round with your words. The crew answers questions straight away.",
  },
  stopped: {
    label: "Talk to the crew",
    placeholder: "Ask what happened, or leave a note for when it resumes",
    quick: ["Why did it stop?"],
    hint: "Notes wait with the build and reach the crew when it resumes.",
  },
  gate: {
    label: "Talk to the crew",
    placeholder: "Ask something, or leave a note for the work still to come",
    quick: ["What are you waiting for?"],
    hint: "Notes wait for the agents that haven't started.",
  },
};

// The server's rule (`crew_chat.classify`), for the button's word and the stuck card's
// draft. `tests/test_crew_messages.py` fails if these lists drift from the server's.
const ASKS = new Set([
  "what", "why", "when", "how", "where", "who", "is", "are", "can", "will", "status",
  "could", "would", "does", "did", "has", "which", "should",
]);
/** "Can you keep all the routes in one file" asks for something done: a note — unless it asks to be told something. */
const NOT_A_REQUEST = [
  "tell", "explain", "say", "show", "describe", "confirm", "check", "let", "see", "be", "know",
  "still", "already", "ever", "really", "not", "remember", "hear", "read", "understand",
];
const REQUEST = new RegExp(`^(can|could|will|would)\\s+(you|we|the crew)\\s+(?!(?:${NOT_A_REQUEST.join("|")})\\b)[a-z]`);

/** How the crew will read it. */
export function readsAsQuestion(text: string): boolean {
  const said = text.trim();
  const low = said.toLowerCase();
  if (REQUEST.test(low)) return false;
  if (said.endsWith("?")) return true;
  const first = /^[a-z]+/.exec(low);
  return !!first && ASKS.has(first[0]);
}

/** "Frontend", "Backend", "QA": an agent's role, short. */
function roleOf(phase: string): string {
  return (AGENT_BY_KEY[phase]?.role ?? phase).replace(/ Engineer$/, "");
}

/** `routes/todos.js` in a reply is a file: set in code. */
function withCode(text: string): ReactNode[] {
  return text.split(/(`[^`]+`)/g).map((part, i) =>
    part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
      <code key={i} className="mono">
        {part.slice(1, -1)}
      </code>
    ) : (
      part
    ),
  );
}

/** What happened to the message, in a few words, and its tone. */
function stateOf(m: CrewMessage): { word: string; tone: "ok" | "run" | "warn" | "plain" } {
  if (m.kind === "question") {
    if (m.status === "queued") return { word: "Answering after this step", tone: "run" };
    if (m.status === "declined") return { word: "Not answered", tone: "plain" };
    return { word: "Answered", tone: "ok" };
  }
  // What it does to finished work comes first while it's pending; once it's made (or not),
  // the note's delivery says the rest.
  if (m.change === "checking") return { word: "Checking finished work", tone: "run" };
  if (m.change === "applied" && (m.touched ?? []).length) {
    return { word: `Changed ${listOf((m.touched ?? []).map(roleOf))}`, tone: "ok" };
  }
  if (m.change === "scheduled") {
    const who = (m.touched ?? []).map(roleOf);
    return { word: who.length ? `Changes ${listOf(who)} after this step` : "Change after this step", tone: "warn" };
  }
  switch (m.status) {
    case "applied": {
      // Each agent it's for takes it in turn (#102): the first few by name, then a count.
      const roles = m.applied_to.map(roleOf);
      const named = roles.length > 3 ? `${roles.slice(0, 2).join(", ")} and ${roles.length - 2} more` : listOf(roles);
      return { word: roles.length ? `Applied to ${named}` : "Applied", tone: "ok" };
    }
    case "cancelled":
      return { word: "Cancelled", tone: "plain" };
    case "declined":
      return { word: "Not sent", tone: "plain" };
    default: {
      // Who it waits for as it's read now; the newest reply's, for a message from before that was kept.
      const who = m.agent ?? [...m.replies].reverse().find((r) => r.agent)?.agent;
      return { word: who ? `Waiting for ${roleOf(who)}` : "Waiting", tone: "run" };
    }
  }
}

/** Arrived since the box was first drawn, and still settling in: it opens its own height. */
function useFresh(ids: string[]): (id: string) => boolean {
  const born = useRef<Map<string, number> | null>(null);
  const now = Date.now();
  if (born.current === null) born.current = new Map(ids.map((i) => [i, 0]));
  for (const i of ids) if (!born.current.has(i)) born.current.set(i, now);
  return (i) => now - (born.current?.get(i) ?? 0) < 700;
}

function Reply({ reply, fresh }: { reply: CrewReply; fresh: boolean }) {
  const agent = reply.agent ? AGENT_BY_KEY[reply.agent] : undefined;
  return (
    <div className={`talk-row${fresh ? " is-new" : ""}`}>
      <div className="talk-row-in">
        <div className="talk-reply" style={agent ? ({ "--agent": agent.accent } as React.CSSProperties) : undefined}>
          <span className="talk-face" aria-hidden="true">
            {agent ? <AgentSprite agent={agent} size={20} state="queued" /> : Icon.sparkle}
          </span>
          <p className="talk-said">
            <b className="talk-name" title={agent?.role}>
              {agent ? agent.codename : "Crew"}
            </b>{" "}
            {withCode(reply.text)}
          </p>
        </div>
      </div>
    </div>
  );
}

export function CrewTalk({
  project,
  id,
  busy,
  act,
  place,
  embedded = false,
  text: heldText,
  setText: setHeldText,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: Act;
  place: TalkPlace;
  /** Inside a card (the stuck one): the card's field, not a surface of its own. */
  embedded?: boolean;
  /** The draft, when the card around the box needs to read it. */
  text?: string;
  setText?: (text: string) => void;
}) {
  const [ownText, setOwnText] = useState("");
  const text = heldText ?? ownText;
  const setText = setHeldText ?? setOwnText;
  const thread = project.messages ?? [];
  const copy = COPY[place];
  const now = useNow(30000);
  const list = useRef<HTMLOListElement>(null);
  const fresh = useFresh(thread.flatMap((m) => [m.id, ...m.replies.map((r) => r.id)]));
  const count = thread.reduce((n, m) => n + 1 + m.replies.length, 0);
  const newest = thread[thread.length - 1];

  // The conversation opens on its newest line and keeps to its foot while lines arrive
  // and open their height — unless the person scrolled up to read, which `pinned`
  // remembers. Growth is watched, not timed: a row grows for as long as it grows.
  const pinned = useRef(true);
  const ours = useRef(false);
  useLayoutEffect(() => {
    const el = list.current;
    if (!el) return;
    const stick = () => {
      if (!pinned.current || el.scrollHeight - el.scrollTop - el.clientHeight < 1) return;
      ours.current = true;
      el.scrollTop = el.scrollHeight;
    };
    stick();
    const watch = new ResizeObserver(stick);
    for (const turn of Array.from(el.children)) watch.observe(turn);
    return () => watch.disconnect();
  }, [count]);

  const asks = text.trim() ? readsAsQuestion(text) : null;
  const submitLabel = asks === null ? "Send" : asks ? "Ask" : place === "stuck" ? "Try again with this note" : "Send note";

  return (
    <section className={`talk${embedded ? " is-embedded" : ""}`} aria-label="Talk to the crew">
      {thread.length > 0 && (
        <ol
          ref={list}
          className="talk-thread"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Your messages to the crew"
          onScroll={(e) => {
            if (ours.current) return void (ours.current = false);
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
          }}
        >
          {thread.map((m) => {
            const state = stateOf(m);
            // The newest only, and not while a model is still answering it.
            const canReread =
              m.id === newest?.id &&
              (m.status === "answered" || m.status === "declined" || (m.status === "queued" && m.kind === "note")) &&
              m.change !== "scheduled" &&
              m.change !== "applied";
            return (
              <li key={m.id} className="talk-turn" data-kind={m.kind}>
                <div className={`talk-row${fresh(m.id) ? " is-new" : ""}`}>
                  <div className="talk-row-in">
                    <p className="talk-you">
                      <span className="talk-you-text">{m.text}</span>
                      <time className="talk-time" dateTime={m.created_at ?? undefined}>
                        {ago(m.created_at, now)}
                      </time>
                    </p>
                  </div>
                </div>
                {m.replies.map((r) => (
                  <Reply key={r.id} reply={r} fresh={fresh(r.id)} />
                ))}
                <p className="talk-meta">
                  <span className={`talk-state tone-${state.tone}`}>
                    {state.tone === "run" && <span className="dot dot-run dot-pulse" aria-hidden="true" />}
                    {state.word}
                  </span>
                  <span className="talk-read">Read as a {m.kind}</span>
                  {m.kind === "note" && m.change === "scheduled" && (
                    <button
                      type="button"
                      className="talk-reread talk-cancel"
                      disabled={busy}
                      onClick={() => act(() => api.cancelMessage(id, m.id, { change: true }))}
                    >
                      Cancel the change
                    </button>
                  )}
                  {m.kind === "note" && m.status === "queued" && m.change !== "applied" && (
                    <button
                      type="button"
                      className="talk-reread talk-cancel"
                      disabled={busy}
                      onClick={() => act(() => api.cancelMessage(id, m.id))}
                    >
                      Take back
                    </button>
                  )}
                  {canReread && (
                    <button
                      type="button"
                      className="talk-reread"
                      disabled={busy}
                      onClick={() =>
                        act(() => api.rereadMessage(id, m.id, m.kind === "question" ? "note" : "question"))
                      }
                    >
                      {m.kind === "question" ? "That was a note" : "That was a question"}
                    </button>
                  )}
                </p>
              </li>
            );
          })}
        </ol>
      )}
      <Composer
        fieldId={`talk-${place}`}
        className="talk-box"
        label={copy.label}
        placeholder={copy.placeholder}
        text={text}
        setText={setText}
        busy={busy}
        examples={copy.quick}
        examplesSend
        submitLabel={submitLabel}
        submitIcon={place === "stuck" && asks === false ? Icon.rotate : Icon.arrowRight}
        onSend={(said) => act(() => api.sendMessage(id, said))}
        hint={copy.hint}
      />
    </section>
  );
}
