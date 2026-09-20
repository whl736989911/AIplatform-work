/**
 * Folder helpers for one knowledge base.
 *
 * The server keeps one ``folder_path`` per document (``""`` is the base root)
 * and derives the folder list from those rows, so a folder exists exactly while
 * a live document sits in it. A listed path may therefore have ancestors the
 * route does not mention; ``buildFolderTree`` synthesises them so a nested
 * folder is reachable without expanding a phantom parent first.
 *
 * ``checkFolderPath`` mirrors the server's ``normalize_folder_path``, so the
 * move dialog refuses a path the PATCH route would answer with 400 instead of
 * sending it and showing a raw error code.
 */

import type {
  KnowledgeDocument,
  KnowledgeFolder,
} from "../../../api/modules/workbuddyKnowledge";

/** Folder path of the base root; every other path is relative to it. */
export const KNOWLEDGE_ROOT_PATH = "";

/** Extension the text export is saved with. */
const TEXT_EXPORT_EXTENSION = ".txt";

/** Server-side ceiling for one folder path (``FolderMoveBody.max_length``). */
const MAX_FOLDER_PATH_LENGTH = 400;

/** Characters no browser accepts in a file name. */
const ILLEGAL_FILENAME_CHARS: Record<string, true> = {
  "\\": true,
  "/": true,
  ":": true,
  "*": true,
  "?": true,
  '"': true,
  "<": true,
  ">": true,
  "|": true,
};

export interface KnowledgeFolderNode {
  /** Folder path; ``""`` is the root. */
  path: string;
  /** Last path segment; empty for the root. */
  name: string;
  /** Live documents directly in this folder. */
  document_count: number;
  children: KnowledgeFolderNode[];
}

/** Why a folder path cannot be sent to the server. */
export type FolderPathIssue =
  | "absolute"
  | "trailing"
  | "empty_segment"
  | "relative_segment"
  | "backslash"
  | "stray_spaces"
  | "too_long";

export type FolderPathCheck =
  | { path: string; issue: null }
  | { path: null; issue: FolderPathIssue };

/** Validate a folder path the way the server does; ``""`` is the root. */
export function checkFolderPath(raw: string): FolderPathCheck {
  const value = raw.trim();
  if (!value) return { path: KNOWLEDGE_ROOT_PATH, issue: null };
  if (value.length > MAX_FOLDER_PATH_LENGTH) {
    return { path: null, issue: "too_long" };
  }
  if (value.startsWith("/")) return { path: null, issue: "absolute" };
  if (value.endsWith("/")) return { path: null, issue: "trailing" };
  for (const segment of value.split("/")) {
    if (!segment) return { path: null, issue: "empty_segment" };
    if (segment === "." || segment === "..") {
      return { path: null, issue: "relative_segment" };
    }
    if (segment.includes("\\")) return { path: null, issue: "backslash" };
    if (segment !== segment.trim())
      return { path: null, issue: "stray_spaces" };
  }
  return { path: value, issue: null };
}

/**
 * The folder tree of one base: the root plus every listed folder, its missing
 * ancestors and its children. Children are ordered by path.
 */
export function buildFolderTree(
  folders: readonly KnowledgeFolder[],
): KnowledgeFolderNode {
  const root: KnowledgeFolderNode = {
    path: KNOWLEDGE_ROOT_PATH,
    name: "",
    document_count: 0,
    children: [],
  };
  const nodes = new Map<string, KnowledgeFolderNode>([[root.path, root]]);
  const ensure = (path: string): KnowledgeFolderNode => {
    const known = nodes.get(path);
    if (known) return known;
    const cut = path.lastIndexOf("/");
    const parent = ensure(cut < 0 ? KNOWLEDGE_ROOT_PATH : path.slice(0, cut));
    const node: KnowledgeFolderNode = {
      path,
      name: path.slice(cut + 1),
      document_count: 0,
      children: [],
    };
    parent.children.push(node);
    nodes.set(path, node);
    return node;
  };

  for (const folder of folders) {
    ensure(folder.path).document_count = folder.document_count;
  }
  sortByPath(root);
  return root;
}

function sortByPath(node: KnowledgeFolderNode): void {
  node.children.sort((left, right) =>
    left.path < right.path ? -1 : left.path > right.path ? 1 : 0,
  );
  for (const child of node.children) sortByPath(child);
}

/** Live documents sitting directly in one folder. */
export function documentsInFolder(
  documents: readonly KnowledgeDocument[],
  path: string,
): KnowledgeDocument[] {
  return documents.filter(
    (document) => (document.folder_path || KNOWLEDGE_ROOT_PATH) === path,
  );
}

/**
 * Attachment name for the text export: the document title plus ``.txt``, with
 * characters no file system accepts replaced by ``_``.
 */
export function documentTextFilename(title: string, fallback: string): string {
  const cleaned = Array.from(title, (char) =>
    char.charCodeAt(0) < 32 || ILLEGAL_FILENAME_CHARS[char] === true
      ? "_"
      : char,
  )
    .join("")
    .trim();
  return `${cleaned || fallback}${TEXT_EXPORT_EXTENSION}`;
}
