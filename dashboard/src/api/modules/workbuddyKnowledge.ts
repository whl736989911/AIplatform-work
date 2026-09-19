/**
 * WorkBuddy knowledge bases (/workbuddy/knowledge) — all paths are relative to
 * ``/api``.
 *
 * Wire contract frozen by the A3 knowledge slice
 * (``src/octop/api/routers/workbuddy_knowledge.py``): the knowledge-base, ACL,
 * bound-upload, document and search routes below. Every success body is wrapped
 * as ``{ data, request_id }``; list payloads carry ``{ items, count }``.
 *
 * No response ever contains a signing secret, an object-store credential or a
 * raw embedding vector, so neither does any type here.
 */

import { request } from "../request";

const BASE = "/v1";

/** Every WorkBuddy success body is wrapped: ``{ data, request_id }``. */
interface ApiEnvelope<T> {
  data: T;
  request_id?: string;
}

async function unwrap<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await request<ApiEnvelope<T>>(path, init);
  return body.data;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

// --- Knowledge bases -------------------------------------------------------

/** personal = owner only, department = current members, enterprise = all members. */
export type KnowledgeScope = "personal" | "department" | "enterprise";

/** Effective permission of the caller on a base; null only for invisible rows. */
export type KnowledgePermission = "read" | "write" | "admin";

/** Embedding descriptor echoed by search (no platform revision id). */
export interface KnowledgeEmbeddingDescriptor {
  adapter_key: string;
  model_key: string;
  revision: number;
  dimensions: number;
}

/** The embedding model pinned into one base, plus its granted platform revision. */
export interface KnowledgeEmbeddingPin extends KnowledgeEmbeddingDescriptor {
  model_revision_id: string;
}

export interface KnowledgeBase {
  kb_id: string;
  name: string;
  description: string;
  scope: KnowledgeScope;
  department_id: string | null;
  owner_user_id: number | null;
  archived_at: number | null;
  permission: KnowledgePermission | null;
  embedding: KnowledgeEmbeddingPin;
  created_at: number;
  updated_at: number;
}

export interface KnowledgeBaseCreate {
  scope: KnowledgeScope;
  name: string;
  description?: string;
  /** Required for the ``department`` scope (UUID). */
  department_id?: string | null;
  /** Published ``bge-m3`` platform revision granted to this tenant. */
  model_revision_id: string;
}

export interface KnowledgeBaseArchived {
  kb_id: string;
  archived: boolean;
  archived_at: number;
}

export interface KnowledgeBaseList {
  items: KnowledgeBase[];
  count: number;
}

// --- ACL -------------------------------------------------------------------

export interface KnowledgeAclRow {
  acl_id: string;
  /** Exactly one of user_id / department_id is set. */
  user_id: number | null;
  department_id: string | null;
  permission: KnowledgePermission;
  created_at: number;
  updated_at: number;
}

export interface KnowledgeAclCreate {
  permission: KnowledgePermission;
  user_id?: number | null;
  department_id?: string | null;
}

export interface KnowledgeAclUpdate {
  permission: KnowledgePermission;
}

export interface KnowledgeAclRevoked {
  acl_id: string;
  revoked: boolean;
}

export interface KnowledgeAclList {
  items: KnowledgeAclRow[];
  count: number;
}

// --- Bound uploads ---------------------------------------------------------

/** ``upload_url`` is deployment-provided; null means bytes go through the store hook. */
export interface KnowledgeUploadTarget {
  object_key: string;
  upload_url: string | null;
}

export interface KnowledgeUploadCreate {
  filename: string;
  /** Must equal the canonical type for the file extension. */
  mime_type: string;
  size_bytes: number;
}

export interface KnowledgeUpload {
  upload_id: string;
  kb_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  status: string;
  expires_at: number;
  upload_target: KnowledgeUploadTarget;
}

export interface KnowledgeUploadComplete {
  /** Optional caller-computed digest of the uploaded object. */
  checksum_sha256?: string | null;
}

/** Bound, scanned file reference returned by upload completion. */
export interface KnowledgeFileRef {
  file_ref_id: string;
  kb_id: string;
  upload_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  checksum_sha256: string;
  created_at: number;
}

// --- Documents -------------------------------------------------------------

/**
 * Document lifecycle: pending → parsing → indexing → ready, or failed with an
 * ``error_code``; ``deleted`` marks a removed (no longer retrievable) row.
 */
export type KnowledgeDocumentStatus =
  | "pending"
  | "parsing"
  | "indexing"
  | "ready"
  | "failed"
  | "deleted";

export interface KnowledgeDocumentCreate {
  upload_id?: string | null;
  /** File reference returned by upload completion (``file_ref_id`` is its alias). */
  file_ref?: string | null;
  file_ref_id?: string | null;
  title?: string;
}

/** 202 response: the document row plus the scheduled indexing job id. */
export interface KnowledgeDocumentCreated {
  document_id: string;
  kb_id: string;
  job_id: string;
  title: string;
  status: string;
  chunk_count: number;
  created_at: number;
}

export interface KnowledgeDocument {
  document_id: string;
  kb_id: string;
  title: string;
  status: string;
  error_code: string | null;
  active_generation_id: string | null;
  chunk_count: number;
  job_id: string;
  created_at: number;
  updated_at: number;
}

export interface KnowledgeDocumentDeleted {
  document_id: string;
  deleted: boolean;
  retrievable: boolean;
}

export interface KnowledgeDocumentList {
  items: KnowledgeDocument[];
  count: number;
}

// --- Search ----------------------------------------------------------------

export interface KnowledgeSearchRequest {
  query: string;
  match_count?: number;
  /** Must equal the base's pinned model when supplied. */
  embedding_model?: string | null;
}

/** One retrieved chunk with its citation metadata. */
export interface KnowledgeSearchHit {
  chunk_id: string;
  document_id: string;
  document_title: string;
  generation_id: string;
  ordinal: number;
  content: string;
  score: number;
}

export interface KnowledgeSearchResult {
  kb_id: string;
  embedding: KnowledgeEmbeddingDescriptor;
  match_count: number;
  hits: KnowledgeSearchHit[];
}

// --- Tenant model catalog (embedding picker) -------------------------------

/**
 * One published model revision approved for this tenant, from the shared
 * read-only route ``GET /v1/model-catalog``. The create form needs the granted
 * ``bge-m3`` revision id, and the page may only reach the API through this
 * module.
 */
export interface KnowledgeModelRevision {
  id: string;
  model_key: string;
  display_name: string;
  revision: number;
  status: string;
}

export interface KnowledgeModelCatalog {
  items: KnowledgeModelRevision[];
}

// --- Upload constraints ----------------------------------------------------

/** Server-side ceiling for one uploaded object (64 MiB). */
export const KNOWLEDGE_MAX_UPLOAD_BYTES = 64 * 1024 * 1024;

/** Extension → canonical media type accepted by the knowledge indexer. */
const CANONICAL_MIME: Record<string, string> = {
  txt: "text/plain",
  md: "text/markdown",
  markdown: "text/markdown",
  csv: "text/csv",
  json: "application/json",
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  zip: "application/zip",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
};

/** Canonical media type for a bare file name, or null when unsupported. */
export function canonicalKnowledgeMime(filename: string): string | null {
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return null;
  return CANONICAL_MIME[filename.slice(dot + 1).toLowerCase()] ?? null;
}

export const workbuddyKnowledgeApi = {
  // Knowledge bases
  listBases: () => unwrap<KnowledgeBaseList>(`${BASE}/knowledge-bases`),
  createBase: (body: KnowledgeBaseCreate) =>
    unwrap<KnowledgeBase>(`${BASE}/knowledge-bases`, jsonInit("POST", body)),
  getBase: (kbId: string) =>
    unwrap<KnowledgeBase>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}`,
    ),
  archiveBase: (kbId: string) =>
    unwrap<KnowledgeBaseArchived>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/archive`,
      jsonInit("POST", {}),
    ),

  // Explicit grants (base admins only)
  listAcl: (kbId: string) =>
    unwrap<KnowledgeAclList>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/acl`,
    ),
  addAcl: (kbId: string, body: KnowledgeAclCreate) =>
    unwrap<KnowledgeAclRow>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/acl`,
      jsonInit("POST", body),
    ),
  updateAcl: (kbId: string, aclId: string, body: KnowledgeAclUpdate) =>
    unwrap<KnowledgeAclRow>(
      `${BASE}/knowledge-bases/${encodeURIComponent(
        kbId,
      )}/acl/${encodeURIComponent(aclId)}`,
      jsonInit("PUT", body),
    ),
  deleteAcl: (kbId: string, aclId: string) =>
    unwrap<KnowledgeAclRevoked>(
      `${BASE}/knowledge-bases/${encodeURIComponent(
        kbId,
      )}/acl/${encodeURIComponent(aclId)}`,
      { method: "DELETE" },
    ),

  // Bound uploads
  createUpload: (kbId: string, body: KnowledgeUploadCreate) =>
    unwrap<KnowledgeUpload>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/uploads`,
      jsonInit("POST", body),
    ),
  completeUpload: (
    kbId: string,
    uploadId: string,
    body: KnowledgeUploadComplete = {},
  ) =>
    unwrap<KnowledgeFileRef>(
      `${BASE}/knowledge-bases/${encodeURIComponent(
        kbId,
      )}/uploads/${encodeURIComponent(uploadId)}/complete`,
      jsonInit("POST", body),
    ),

  // Documents
  listDocuments: (kbId: string) =>
    unwrap<KnowledgeDocumentList>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/documents`,
    ),
  createDocument: (kbId: string, body: KnowledgeDocumentCreate) =>
    unwrap<KnowledgeDocumentCreated>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/documents`,
      jsonInit("POST", body),
    ),
  deleteDocument: (kbId: string, documentId: string) =>
    unwrap<KnowledgeDocumentDeleted>(
      `${BASE}/knowledge-bases/${encodeURIComponent(
        kbId,
      )}/documents/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
    ),

  // Search
  search: (kbId: string, body: KnowledgeSearchRequest) =>
    unwrap<KnowledgeSearchResult>(
      `${BASE}/knowledge-bases/${encodeURIComponent(kbId)}/search`,
      jsonInit("POST", body),
    ),

  // Tenant-approved model revisions (shared read-only catalog route)
  modelCatalog: () => unwrap<KnowledgeModelCatalog>(`${BASE}/model-catalog`),
};
