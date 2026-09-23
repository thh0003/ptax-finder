import {
  CognitoIdentityProviderClient,
  InitiateAuthCommand,
  RespondToAuthChallengeCommand,
  type AuthenticationResultType,
  type CognitoIdentityProviderClientConfig,
} from "@aws-sdk/client-cognito-identity-provider";

export type Tokens = {
  accessToken: string;
  idToken?: string;
  refreshToken?: string;
  /** Epoch milliseconds at which the access token expires. */
  expiresAt: number;
};

export type SignInResult =
  | { kind: "tokens"; tokens: Tokens }
  | { kind: "new_password_required"; session: string; email: string };

type ClientSettings = { aws_region: string; cognito_endpoint_url?: string | null };

/** Endpoint is set only for cognito-local; in AWS the SDK derives it from the region. */
export function clientConfig(settings: ClientSettings): CognitoIdentityProviderClientConfig {
  const config: CognitoIdentityProviderClientConfig = { region: settings.aws_region };
  if (settings.cognito_endpoint_url) config.endpoint = settings.cognito_endpoint_url;
  return config;
}

export function makeClient(settings: ClientSettings): CognitoIdentityProviderClient {
  return new CognitoIdentityProviderClient(clientConfig(settings));
}

function toTokens(result: AuthenticationResultType | undefined, previousRefresh?: string): Tokens {
  if (!result?.AccessToken) throw new Error("Cognito returned no access token");
  return {
    accessToken: result.AccessToken,
    idToken: result.IdToken,
    refreshToken: result.RefreshToken ?? previousRefresh,
    expiresAt: Date.now() + (result.ExpiresIn ?? 3600) * 1000,
  };
}

export async function signIn(
  client: CognitoIdentityProviderClient,
  clientId: string,
  email: string,
  password: string,
): Promise<SignInResult> {
  const response = await client.send(
    new InitiateAuthCommand({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: clientId,
      AuthParameters: { USERNAME: email, PASSWORD: password },
    }),
  );
  if (response.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    return { kind: "new_password_required", session: response.Session ?? "", email };
  }
  return { kind: "tokens", tokens: toTokens(response.AuthenticationResult) };
}

export async function respondNewPassword(
  client: CognitoIdentityProviderClient,
  clientId: string,
  email: string,
  newPassword: string,
  session: string,
): Promise<Tokens> {
  const response = await client.send(
    new RespondToAuthChallengeCommand({
      ClientId: clientId,
      ChallengeName: "NEW_PASSWORD_REQUIRED",
      Session: session,
      ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
    }),
  );
  return toTokens(response.AuthenticationResult);
}

export async function refresh(
  client: CognitoIdentityProviderClient,
  clientId: string,
  refreshToken: string,
): Promise<Tokens> {
  const response = await client.send(
    new InitiateAuthCommand({
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: clientId,
      AuthParameters: { REFRESH_TOKEN: refreshToken },
    }),
  );
  return toTokens(response.AuthenticationResult, refreshToken);
}
