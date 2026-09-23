import { describe, expect, it, vi } from "vitest";
import {
  InitiateAuthCommand,
  RespondToAuthChallengeCommand,
} from "@aws-sdk/client-cognito-identity-provider";
import { clientConfig, refresh, respondNewPassword, signIn } from "./cognito";

const CLIENT_ID = "abc123";

function fakeClient(response: unknown) {
  const send = vi.fn().mockResolvedValue(response);
  return { client: { send } as never, send };
}

describe("signIn", () => {
  it("returns tokens with an absolute expiry on success", async () => {
    const { client, send } = fakeClient({
      AuthenticationResult: {
        AccessToken: "access",
        IdToken: "id",
        RefreshToken: "refresh",
        ExpiresIn: 3600,
      },
    });
    const before = Date.now();

    const result = await signIn(client, CLIENT_ID, "a@demo.test", "Password1!");

    expect(result.kind).toBe("tokens");
    if (result.kind !== "tokens") throw new Error("unreachable");
    expect(result.tokens.accessToken).toBe("access");
    expect(result.tokens.refreshToken).toBe("refresh");
    expect(result.tokens.expiresAt).toBeGreaterThanOrEqual(before + 3600_000);

    const command = send.mock.calls[0][0] as InitiateAuthCommand;
    expect(command).toBeInstanceOf(InitiateAuthCommand);
    expect(command.input).toEqual({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: CLIENT_ID,
      AuthParameters: { USERNAME: "a@demo.test", PASSWORD: "Password1!" },
    });
  });

  it("surfaces the NEW_PASSWORD_REQUIRED challenge with its session", async () => {
    const { client } = fakeClient({ ChallengeName: "NEW_PASSWORD_REQUIRED", Session: "sess-1" });

    const result = await signIn(client, CLIENT_ID, "new@demo.test", "Temp1!");

    expect(result).toEqual({ kind: "new_password_required", session: "sess-1", email: "new@demo.test" });
  });
});

describe("respondNewPassword", () => {
  it("answers the challenge and returns tokens", async () => {
    const { client, send } = fakeClient({
      AuthenticationResult: { AccessToken: "a2", RefreshToken: "r2", ExpiresIn: 60 },
    });

    const tokens = await respondNewPassword(client, CLIENT_ID, "new@demo.test", "Perm1!", "sess-1");

    expect(tokens.accessToken).toBe("a2");
    const command = send.mock.calls[0][0] as RespondToAuthChallengeCommand;
    expect(command).toBeInstanceOf(RespondToAuthChallengeCommand);
    expect(command.input).toEqual({
      ClientId: CLIENT_ID,
      ChallengeName: "NEW_PASSWORD_REQUIRED",
      Session: "sess-1",
      ChallengeResponses: { USERNAME: "new@demo.test", NEW_PASSWORD: "Perm1!" },
    });
  });
});

describe("refresh", () => {
  it("keeps the existing refresh token when Cognito omits it", async () => {
    const { client, send } = fakeClient({
      AuthenticationResult: { AccessToken: "a3", ExpiresIn: 3600 },
    });

    const tokens = await refresh(client, CLIENT_ID, "r-old");

    expect(tokens.accessToken).toBe("a3");
    expect(tokens.refreshToken).toBe("r-old");
    const command = send.mock.calls[0][0] as InitiateAuthCommand;
    expect(command.input.AuthFlow).toBe("REFRESH_TOKEN_AUTH");
    expect(command.input.AuthParameters).toEqual({ REFRESH_TOKEN: "r-old" });
  });
});

describe("clientConfig", () => {
  it("sets the endpoint only when the backend config provides one", () => {
    expect(clientConfig({ aws_region: "us-east-1", cognito_endpoint_url: null })).toEqual({
      region: "us-east-1",
    });
    expect(
      clientConfig({ aws_region: "us-east-1", cognito_endpoint_url: "http://localhost:9229" }),
    ).toEqual({ region: "us-east-1", endpoint: "http://localhost:9229" });
  });
});
