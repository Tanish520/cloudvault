import { useCallback, useEffect, useState } from "react";
import { Hub } from "aws-amplify/utils";
import { getAuthenticatedUser } from "./auth/session";
import AuthPanel from "./components/AuthPanel";
import FileList from "./components/FileList";
import FileUpload from "./components/FileUpload";
import { getFiles } from "./services/api";
import "./App.css";

export default function App() {
  const [user, setUser] = useState(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadSession = useCallback(async () => {
    setCheckingAuth(true);
    try {
      setUser(await getAuthenticatedUser());
    } catch {
      setUser(null);
      setFiles([]);
    } finally {
      setCheckingAuth(false);
    }
  }, []);

  const loadFiles = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    setError("");
    try {
      const result = await getFiles();
      setFiles(result.files ?? []);
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    loadSession();
    return Hub.listen("auth", ({ payload }) => {
      if (["signedIn", "signedOut"].includes(payload.event)) {
        loadSession();
      }
    });
  }, [loadSession]);

  useEffect(() => {
    if (user) loadFiles();
  }, [user, loadFiles]);

  return (
    <main>
      <header>
        <div>
          <h1>CloudVault</h1>
          <p>Private serverless file storage on AWS.</p>
        </div>
        {user && <AuthPanel user={user} onAuthChanged={loadSession} />}
      </header>

      {checkingAuth ? (
        <p className="status">Checking your session...</p>
      ) : user ? (
        <>
          <FileUpload onUploaded={loadFiles} />
          {error && <p className="error" role="alert">{error}</p>}
          <FileList files={files} loading={loading} onChanged={loadFiles} />
        </>
      ) : (
        <AuthPanel user={null} onAuthChanged={loadSession} />
      )}
    </main>
  );
}
