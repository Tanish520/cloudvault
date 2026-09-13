import { useState } from "react";
import {
  confirmUpload,
  requestUploadUrl,
  uploadFileToS3,
} from "../services/api";

const MAX_FILE_SIZE = 10 * 1024 * 1024;

const ALLOWED_TYPES = [
  "application/pdf",
  "image/png",
  "image/jpeg",
  "text/plain",
];

export default function FileUpload({ onUploaded }) {
  const [file, setFile] = useState(null);
  const [status, setStatus] = useState("");
  const [uploading, setUploading] = useState(false);

  function chooseFile(event) {
    setFile(event.target.files?.[0] ?? null);
    setStatus("");
  }

  async function handleUpload() {
    if (!file) {
      setStatus("Choose a file first.");
      return;
    }

    // Client-side checks are a courtesy — S3 and Lambda enforce the real limits.
    if (!ALLOWED_TYPES.includes(file.type)) {
      setStatus("Unsupported file type. Allowed: PDF, PNG, JPEG, TXT.");
      return;
    }

    if (file.size > MAX_FILE_SIZE) {
      setStatus("Maximum file size is 10 MB.");
      return;
    }

    setUploading(true);

    try {
      setStatus("Requesting upload permission...");
      const uploadData = await requestUploadUrl(file);

      setStatus("Uploading to S3...");
      await uploadFileToS3(uploadData, file);

      setStatus("Saving file information...");
      await confirmUpload(uploadData, file);

      setStatus(`Uploaded ${uploadData.fileName}.`);
      setFile(null);
      await onUploaded();
    } catch (error) {
      setStatus(error.message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <section className="panel">
      <h2>Upload a file</h2>

      <div className="upload-row">
        <input
          type="file"
          accept=".pdf,.png,.jpg,.jpeg,.txt"
          disabled={uploading}
          onChange={chooseFile}
        />

        <button type="button" disabled={!file || uploading} onClick={handleUpload}>
          {uploading ? "Uploading..." : "Upload"}
        </button>
      </div>

      {status && <p className="status">{status}</p>}
    </section>
  );
}
