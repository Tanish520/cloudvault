import { signInWithRedirect, signOut } from "aws-amplify/auth";

export default function AuthPanel({ user, onAuthChanged }) {
  async function handleSignOut() {
    await signOut();
    await onAuthChanged();
  }

  if (!user) {
    return (
      <section className="panel auth-panel">
        <h2>Private file storage</h2>
        <p>Create an account or sign in to access your files.</p>
        <button type="button" onClick={() => signInWithRedirect()}>
          Sign in or create account
        </button>
      </section>
    );
  }

  return (
    <div className="account-row">
      <span>{user.email}</span>
      <button type="button" className="secondary" onClick={handleSignOut}>
        Sign out
      </button>
    </div>
  );
}
