/**
 * Knowledge document panel: folder navigation, extracted-text preview, the
 * plain-text export and reindexing.
 *
 * The API module is exercised for real — the fake below answers the request
 * paths the panel actually asks for — so the assertions cover what leaves the
 * browser (`limit=`, `download=true`, the PATCH body) and what the reader sees,
 * not which internal handler fired.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";
import type {
  KnowledgeBase,
  KnowledgeDocument,
  KnowledgeFolder,
  KnowledgeFolderMove,
  KnowledgePermission,
  KnowledgeReindexFailure,
} from "../../../api/modules/workbuddyKnowledge";

const { messageError, messageSuccess, messageWarning } = vi.hoisted(() => ({
  messageError: vi.fn(),
  messageSuccess: vi.fn(),
  messageWarning: vi.fn(),
}));

vi.mock("@/utils/antdMessage", () => ({
  message: {
    success: (...args: unknown[]) => messageSuccess(...args),
    error: (...args: unknown[]) => messageError(...args),
    warning: (...args: unknown[]) => messageWarning(...args),
  },
}));

vi.mock("../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api/request")>();
  return { ...actual, request: vi.fn(), requestBlob: vi.fn() };
});

import { request, requestBlob } from "../../../api/request";
import DocumentsPanel from "./DocumentsPanel";
import { PREVIEW_LIMIT } from "./DocumentPreviewDrawer";

const KB_ID = "11111111-1111-4111-8111-111111111111";
const TEXT_PATH = `/v1/knowledge-bases/${KB_ID}/documents/doc-root/text`;
const PATCH_PATH = `/v1/knowledge-bases/${KB_ID}/documents/doc-root/folder`;

/** The document is longer than the preview ceiling, so the server cuts it. */
const FULL_TEXT = "运维手册正文。".repeat(800);

function knowledgeBase(permission: KnowledgePermission): KnowledgeBase {
  return {
    kb_id: KB_ID,
    name: "运维知识库",
    description: "运行手册与流程",
    scope: "personal",
    department_id: null,
    owner_user_id: 7,
    archived_at: null,
    permission,
    embedding: {
      adapter_key: "bge",
      model_key: "bge-m3",
      revision: 4,
      dimensions: 1024,
      model_revision_id: "rev-4",
    },
    created_at: 1_700_000_000,
    updated_at: 1_700_000_500,
  };
}

function knowledgeDocument(
  overrides: Pick<KnowledgeDocument, "document_id" | "title" | "folder_path">,
): KnowledgeDocument {
  return {
    kb_id: KB_ID,
    status: "ready",
    error_code: null,
    active_generation_id: "gen-0001",
    chunk_count: 4,
    job_id: "job-0001",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    ...overrides,
  };
}

interface FakeKnowledgeServer {
  documents: KnowledgeDocument[];
  /** ``failed`` entries the base-wide reindex reports back. */
  reindexFailed: KnowledgeReindexFailure[];
}

// The panel's move test mutates the row it moves, so every test builds its own
// rows instead of sharing one object.
function rootDocument(): KnowledgeDocument {
  return knowledgeDocument({
    document_id: "doc-root",
    title: "根目录手册",
    folder_path: "",
  });
}

function runbookDocument(): KnowledgeDocument {
  return knowledgeDocument({
    document_id: "doc-runbook",
    title: "运行手册",
    folder_path: "Ops/Runbooks",
  });
}

function serverWith(
  documents: KnowledgeDocument[],
  reindexFailed: KnowledgeReindexFailure[] = [],
): FakeKnowledgeServer {
  return { documents, reindexFailed };
}

/** Folders exist while a document sits in them; the root is always listed. */
function foldersOf(documents: KnowledgeDocument[]): KnowledgeFolder[] {
  const counts = new Map<string, number>([["", 0]]);
  for (const document of documents) {
    counts.set(
      document.folder_path,
      (counts.get(document.folder_path) ?? 0) + 1,
    );
  }
  return Array.from(counts, ([path, document_count]) => ({
    path,
    document_count,
  }));
}

function fakeRequest(server: FakeKnowledgeServer): typeof request {
  const envelope = (data: unknown) => ({ data, request_id: "test-request" });
  return (async (path: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const scoped = /\/documents\/([^/?]+)\/(text|folder|reindex)/.exec(path);
    const documentId = scoped ? decodeURIComponent(scoped[1]) : null;

    if (path === "/settings/timezone") return { timezone: "UTC" };
    if (method === "GET" && path.endsWith("/documents")) {
      return envelope({
        items: server.documents,
        count: server.documents.length,
      });
    }
    if (method === "GET" && path.endsWith("/folders")) {
      return envelope({ kb_id: KB_ID, folders: foldersOf(server.documents) });
    }
    if (method === "PATCH" && scoped?.[2] === "folder") {
      const body = JSON.parse(
        String(init?.body ?? "{}"),
      ) as KnowledgeFolderMove;
      const moved = server.documents.find(
        (document) => document.document_id === documentId,
      );
      if (moved) moved.folder_path = body.folder_path;
      return envelope({
        document_id: documentId,
        kb_id: KB_ID,
        folder_path: body.folder_path,
      });
    }
    if (method === "GET" && scoped?.[2] === "text") {
      const query = new URLSearchParams(path.slice(path.indexOf("?") + 1));
      const raw = query.get("limit");
      const limit = raw === null ? undefined : Number(raw);
      const text = limit === undefined ? FULL_TEXT : FULL_TEXT.slice(0, limit);
      const document = server.documents.find(
        (row) => row.document_id === documentId,
      );
      return envelope({
        document_id: documentId,
        kb_id: KB_ID,
        title: document?.title ?? "",
        text,
        truncated: text.length < FULL_TEXT.length,
        chunk_count: document?.chunk_count ?? 0,
      });
    }
    if (method === "POST" && scoped?.[2] === "reindex") {
      return envelope({
        document_id: documentId,
        kb_id: KB_ID,
        job_id: "job-9000",
        reindexed: true,
      });
    }
    if (method === "POST" && path.endsWith("/reindex")) {
      return envelope({
        kb_id: KB_ID,
        queued: server.documents.length,
        failed: server.reindexFailed,
      });
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  }) as typeof request;
}

function mount(
  server: FakeKnowledgeServer,
  permission: KnowledgePermission = "write",
) {
  vi.mocked(request).mockImplementation(fakeRequest(server));
  return render(<DocumentsPanel base={knowledgeBase(permission)} />);
}

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

/** Anchor-click spy of the download case, restored after it runs. */
let clickSpy: MockInstance | null = null;

beforeEach(() => {
  vi.mocked(request).mockReset();
  vi.mocked(requestBlob).mockReset();
  messageError.mockReset();
  messageSuccess.mockReset();
  messageWarning.mockReset();
});

afterEach(() => {
  // Only the prototype spy is undone here: ``vi.restoreAllMocks()`` also resets
  // the setup file's ``window.matchMedia`` mock, which antd's breakpoint
  // observer needs on the very next render.
  clickSpy?.mockRestore();
  clickSpy = null;
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

describe("knowledge documents panel", () => {
  it("shows one folder at a time and lets the reader walk the tree", async () => {
    mount(serverWith([rootDocument(), runbookDocument()]));

    // The root view holds the root document only, and the tree names the folder
    // the other one lives in.
    await screen.findByText("根目录手册");
    expect(screen.queryByText("运行手册")).toBeNull();
    fireEvent.click(await screen.findByText("Runbooks"));

    await screen.findByText("运行手册");
    expect(screen.queryByText("根目录手册")).toBeNull();
  });

  it("moves a document into a typed folder and refreshes both lists", async () => {
    const server = serverWith([rootDocument(), runbookDocument()]);
    mount(server);

    await screen.findByText("根目录手册");
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.folders.move",
      }),
    );
    fireEvent.change(await screen.findByRole("combobox"), {
      target: { value: "Ops/Runbooks" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.common.confirm",
      }),
    );

    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.some(([, init]) => init?.method === "PATCH"),
      ).toBe(true),
    );
    const patch = vi
      .mocked(request)
      .mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(patch?.[0]).toBe(PATCH_PATH);
    expect(patch?.[1]?.body).toBe(
      JSON.stringify({ folder_path: "Ops/Runbooks" }),
    );

    // The move refreshes both lists: the documents and the folder tree.
    await waitFor(() => {
      const paths = vi
        .mocked(request)
        .mock.calls.map(([path]) => String(path))
        .filter(
          (path) => path.endsWith("/documents") || path.endsWith("/folders"),
        );
      expect(paths.filter((path) => path.endsWith("/documents"))).toHaveLength(
        2,
      );
      expect(paths.filter((path) => path.endsWith("/folders"))).toHaveLength(2);
    });

    // The refreshed list drops it from the folder it left…
    await waitFor(() => expect(screen.queryByText("根目录手册")).toBeNull());
    // …and the folder it entered now holds it.
    fireEvent.click(screen.getByText("Runbooks"));
    await screen.findByText("根目录手册");
    expect(messageError).not.toHaveBeenCalled();
  });

  it("refuses a folder path the server would reject, without calling the route", async () => {
    mount(serverWith([rootDocument()]));

    await screen.findByText("根目录手册");
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.folders.move",
      }),
    );
    fireEvent.change(await screen.findByRole("combobox"), {
      target: { value: "Ops//Runbooks" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.common.confirm",
      }),
    );

    await screen.findByText("workbuddy.knowledge.folders.issue.empty_segment");
    expect(
      vi
        .mocked(request)
        .mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);
    expect(messageError).not.toHaveBeenCalled();
  });

  it("previews the indexed text with a limit and says when it was cut short", async () => {
    mount(serverWith([rootDocument()]));

    fireEvent.click(await screen.findByRole("button", { name: "根目录手册" }));

    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.some(([path]) =>
            String(path).includes(`${TEXT_PATH}?limit=${PREVIEW_LIMIT}`),
          ),
      ).toBe(true),
    );
    await screen.findByText("workbuddy.knowledge.preview.truncated");
    expect(screen.getByText(FULL_TEXT.slice(0, PREVIEW_LIMIT))).toBeTruthy();
  });

  it("downloads the extracted text through the attachment route", async () => {
    const createObjectURL = vi.fn(() => "blob:extracted-text");
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = vi.fn();
    clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    vi.mocked(requestBlob).mockResolvedValue(
      new Blob([FULL_TEXT], { type: "text/plain" }),
    );
    mount(serverWith([rootDocument()]));

    await screen.findByText("根目录手册");
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.documents.download",
      }),
    );

    await waitFor(() =>
      expect(vi.mocked(requestBlob)).toHaveBeenCalledTimes(1),
    );
    expect(vi.mocked(requestBlob).mock.calls[0][0]).toBe(
      `${TEXT_PATH}?download=true`,
    );
    await waitFor(() => expect(clickSpy).toHaveBeenCalled());
    expect(
      (clickSpy.mock.instances[0] as unknown as HTMLAnchorElement).download,
    ).toBe("根目录手册.txt");
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(messageError).not.toHaveBeenCalled();
  });

  it("shows every refused document when the whole base is reindexed", async () => {
    mount(
      serverWith(
        [rootDocument(), runbookDocument()],
        [{ document_id: "doc-broken", code: "WORKBUDDY_INVALID_ARGUMENT" }],
      ),
    );

    await screen.findByText("根目录手册");
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.reindex.base",
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "workbuddy.knowledge.common.confirm",
      }),
    );

    expect(await screen.findByText("doc-broken")).toBeTruthy();
    expect(screen.getByText("WORKBUDDY_INVALID_ARGUMENT")).toBeTruthy();
    expect(
      screen.getByText("workbuddy.knowledge.reindex.baseFailuresTitle"),
    ).toBeTruthy();
    expect(
      vi.mocked(request).mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(true);
    expect(messageWarning).toHaveBeenCalled();
  });

  it("reindexes one document from its own row", async () => {
    mount(serverWith([rootDocument()]));

    await screen.findByText("根目录手册");
    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.reindex.document",
      }),
    );

    await waitFor(() =>
      expect(
        vi
          .mocked(request)
          .mock.calls.some(
            ([path, init]) =>
              String(path) ===
                `/v1/knowledge-bases/${KB_ID}/documents/doc-root/reindex` &&
              init?.method === "POST",
          ),
      ).toBe(true),
    );
    expect(messageSuccess).toHaveBeenCalled();
    expect(messageError).not.toHaveBeenCalled();
  });

  it("keeps readers to the read-only affordances", async () => {
    mount(serverWith([rootDocument()]), "read");

    await screen.findByText("根目录手册");
    expect(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.documents.download",
      }),
    ).toBeTruthy();
    expect(
      screen.getByText("workbuddy.knowledge.documents.readOnlyTitle"),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "workbuddy.knowledge.folders.move",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "workbuddy.knowledge.reindex.base",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "workbuddy.knowledge.documents.remove",
      }),
    ).toBeNull();
  });
});
