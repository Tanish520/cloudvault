import { fetchAuthSession, getCurrentUser } from "aws-amplify/auth";


export function userFromSession(session) {
  const email = session.tokens?.idToken?.payload?.email;

  return {
    email: typeof email === "string" ? email : "Signed-in user",
  };
}


export async function getAuthenticatedUser() {
  await getCurrentUser();
  return userFromSession(await fetchAuthSession());
}
