import { fetchAuthSession } from "aws-amplify/auth";

export const API_URL = import.meta.env.VITE_API_URL;

export async function getAccessToken() {
  const session = await fetchAuthSession();
  const token = session.tokens?.accessToken?.toString();

  if (!token) {
    throw new Error("Sign in to continue.");
  }

  return token;
}

async function authenticatedFetch(path, options = {}) {
  const token = await getAccessToken();
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);

  return fetch(`${API_URL}${path}`, { ...options, headers });
}

async function parseResponse(response) {
  const body = await response.json().catch(() => ({}));

  if (response.status === 401) {
    throw new Error("Your session has expired. Sign in again.");
  }

  if (response.status === 429) {
    throw new Error("Too many requests. Please wait a moment and try again.");
  }

  if (!response.ok) {
    throw new Error(body.message || `Request failed (${response.status})`);
  }

  return body;
}

export async function requestUploadUrl(file) {
  const response = await authenticatedFetch("/files/upload-url", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fileName: file.name,
      contentType: file.type,
      size: file.size,
    }),
  });

  return parseResponse(response);
}

export async function uploadFileToS3(uploadData, file) {
  const form = new FormData();

  Object.entries(uploadData.fields).forEach(([key, value]) => {
    form.append(key, value);
  });

  // The file must be appended last — S3 ignores any field that follows it.
  form.append("file", file);

  // No Content-Type header: the browser must set the multipart boundary itself.
  const response = await fetch(uploadData.uploadUrl, {
    method: "POST",
    body: form,
  });

  // A successful presigned POST returns 204 with an empty body.
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      detail.includes("EntityTooLarge")
        ? "File is larger than the upload policy allows"
        : `S3 upload failed (${response.status})`,
    );
  }
}

export async function confirmUpload(uploadData, file) {
  const response = await authenticatedFetch("/files/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fileId: uploadData.fileId,
      fileName: uploadData.fileName,
      s3Key: uploadData.s3Key,
      contentType: file.type,
      size: file.size,
    }),
  });

  return parseResponse(response);
}

export async function getFiles() {
  return parseResponse(await authenticatedFetch("/files"));
}

export async function getDownloadUrl(fileId) {
  const response = await authenticatedFetch(
    `/files/${encodeURIComponent(fileId)}/download`,
  );

  return parseResponse(response);
}

export async function deleteFile(fileId) {
  const response = await authenticatedFetch(
    `/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  );

  return parseResponse(response);
}
