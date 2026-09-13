import { useState } from "react";
import { deleteFile, getDownloadUrl } from "../services/api";

function formatBytes(bytes) {
  if (!bytes) {
    return "0 B";
  }

  const units = ["B", "KB", "MB", "GB"];
  const index = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / 1024 ** index;

  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export default function FileList({ files, loading, onChanged }) {
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");

  async function handleDownload(fileId) {
    setBusyId(fileId);
    setError("");

    try {
      const { downloadUrl } = await getDownloadUrl(fileId);
      window.location.assign(downloadUrl);
    } catch (downloadError) {
      setError(downloadError.message);
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(fileId, fileName) {
    if (!window.confirm(`Delete ${fileName}? This cannot be undone.`)) {
      return;
    }

    setBusyId(fileId);
    setError("");

    try {
      await deleteFile(fileId);
      await onChanged();
    } catch (deleteError) {
      setError(deleteError.message);
    } finally {
      setBusyId(null);
    }
  }

  if (loading) {
    return <p className="status">Loading files...</p>;
  }

  if (files.length === 0) {
    return <p className="status">No files uploaded yet.</p>;
  }

  return (
    <section className="panel">
      <h2>Your files</h2>

      {error && <p className="error" role="alert">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Size</th>
            <th>Uploaded</th>
            <th>Actions</th>
          </tr>
        </thead>

        <tbody>
          {files.map((file) => (
            <tr key={file.fileId}>
              <td>{file.fileName}</td>
              <td>{formatBytes(Number(file.size))}</td>
              <td>{new Date(file.uploadedAt).toLocaleString()}</td>
              <td className="actions">
                <button
                  type="button"
                  disabled={busyId === file.fileId}
                  onClick={() => handleDownload(file.fileId)}
                >
                  Download
                </button>

                <button
                  type="button"
                  className="danger"
                  disabled={busyId === file.fileId}
                  onClick={() => handleDelete(file.fileId, file.fileName)}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
