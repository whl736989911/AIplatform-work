/**
 * The chat-trigger page (A-17).
 *
 * Two promises are worth pinning, and both are about being trustworthy rather
 * than looking nice: the message carries an id generated here (so a resend cannot
 * run the workflow twice), and the page shows what a conversation *caused* —
 * including the ones that did not run and why — instead of only reporting "sent".
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const calls: Array<{ path: string; method: string; body: unknown }> = [];

const request = vi.fn(
  async (path: string, init?: { method?: string; body?: unknown }) => {
    const method = init?.method ?? "GET";
    calls.push({ path, method, body: init?.body });
    if (method === "POST") {
      return {
        data: {
          conversation_id: "conv-1",
          message_id: "m-1",
          matched: 1,
          deliveries: [],
        },
      };
    }
    return {
      data: {
        conversation_id: "conv-1",
        items: [
          {
            delivery_id: "d-1",
            registration_id: "r-1",
            event_key: "chat:conv-1:m-1",
            status: "executed",
            execution_id: "12345678-1234-4123-8123-123456789012",
            rejection_code: null,
            received_at: 1_700_000_000,
            completed_at: 1_700_000_010,
          },
          {
            delivery_id: "d-2",
            registration_id: "r-2",
            event_key: "chat:conv-1:m-2",
            status: "rejected",
            execution_id: null,
            rejection_code: "DISPATCHER_UNAVAILABLE",
            received_at: 1_700_000_000,
            completed_at: null,
          },
        ],
      },
    };
  },
);

vi.mock("../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("../../../api/request")
  >();
  return {
    ...actual,
    request: (path: string, init?: { method?: string; body?: unknown }) =>
      request(path, init),
  };
});

import WorkBuddyChatflowPage from "./index";

beforeEach(() => {
  calls.length = 0;
  request.mockClear();
});

async function sendAMessage(user: ReturnType<typeof userEvent.setup>) {
  await user.type(
    screen.getByPlaceholderText(/workbuddy\.chatflow\.conversationPlaceholder/),
    "conv-1",
  );
  await user.type(
    screen.getByPlaceholderText(/workbuddy\.chatflow\.messagePlaceholder/),
    "帮我查一下报价",
  );
  const send = screen.getByRole("button", {
    name: /workbuddy\.chatflow\.send/,
  });
  await waitFor(() => expect((send as HTMLButtonElement).disabled).toBe(false));
  await user.click(send);
}

describe("WorkBuddyChatflowPage", () => {
  it("sends the message with an id generated for it", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <WorkBuddyChatflowPage />
      </MemoryRouter>,
    );
    await sendAMessage(user);

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST")).toBe(true),
    );
    const post = calls.find((call) => call.method === "POST");
    const body = JSON.parse(String(post?.body)) as {
      message_id?: string;
      text?: string;
    };
    // The id travels with the message because it is the dedupe key: a resend of
    // the same message must not run the workflow a second time.
    expect(typeof body.message_id).toBe("string");
    expect(body.message_id).not.toHaveLength(0);
    expect(body.text).toBe("帮我查一下报价");
    expect(post?.path).toBe("/v1/conversations/conv-1/messages");
  });

  it("shows what the conversation triggered, and why nothing ran", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <WorkBuddyChatflowPage />
      </MemoryRouter>,
    );
    await sendAMessage(user);

    // The run a message started, and the delivery that was refused — with the
    // reason, because "nothing happened" is not an explanation.
    expect(await screen.findByText("executed")).toBeTruthy();
    expect(screen.getByText("rejected")).toBeTruthy();
    expect(screen.getByText("DISPATCHER_UNAVAILABLE")).toBeTruthy();
    expect(screen.getByText("12345678")).toBeTruthy();
  });
});
