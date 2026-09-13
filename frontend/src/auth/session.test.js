import assert from "node:assert/strict";
import test from "node:test";

import { userFromSession } from "./session.js";


test("uses the verified email claim from the ID token", () => {
  const user = userFromSession({
    tokens: {
      idToken: { payload: { email: "user@example.com" } },
    },
  });

  assert.deepEqual(user, { email: "user@example.com" });
});


test("uses a safe label when the ID token has no email claim", () => {
  const user = userFromSession({
    tokens: { idToken: { payload: {} } },
  });

  assert.deepEqual(user, { email: "Signed-in user" });
});
