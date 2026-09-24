import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as cxapi from "aws-cdk-lib/cx-api";
import { buildStacks } from "../lib/app";

// DockerImageAsset hashes the repo context; point it at a stable stand-in for tests.
const app = new cdk.App();
const stacks = buildStacks(app, { imageDirectory: __dirname + "/fixtures/image" });
const data = Template.fromStack(stacks.data);
const auth = Template.fromStack(stacks.auth);
const compute = Template.fromStack(stacks.compute);
const network = Template.fromStack(stacks.network);

test("VPC has two AZs and a single NAT gateway", () => {
  network.resourceCountIs("AWS::EC2::NatGateway", 1);
  network.resourceCountIs("AWS::EC2::Subnet", 4);
});

test("Aurora is PostgreSQL 16 serverless v2 scaling down to 0 ACU", () => {
  data.hasResourceProperties("AWS::RDS::DBCluster", {
    Engine: "aurora-postgresql",
    EngineVersion: Match.stringLikeRegexp("^16\\."),
    DatabaseName: "ptax",
    ServerlessV2ScalingConfiguration: { MinCapacity: 0, MaxCapacity: 4 },
  });
  data.hasResourceProperties("AWS::RDS::DBInstance", { DBInstanceClass: "db.serverless" });
});

test("uploads bucket is private, versioned, and allows browser PUTs", () => {
  data.hasResourceProperties("AWS::S3::Bucket", {
    VersioningConfiguration: { Status: "Enabled" },
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, RestrictPublicBuckets: true },
    CorsConfiguration: {
      CorsRules: Match.arrayWith([Match.objectLike({ AllowedMethods: Match.arrayWith(["PUT"]) })]),
    },
  });
});

test("user pool client enables USER_PASSWORD_AUTH and sign-up is closed", () => {
  auth.hasResourceProperties("AWS::Cognito::UserPool", {
    AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
    UsernameAttributes: ["email"],
  });
  auth.hasResourceProperties("AWS::Cognito::UserPoolClient", {
    ExplicitAuthFlows: Match.arrayWith(["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]),
    GenerateSecret: false,
  });
});

test("API target group health-checks /api/health and the API service runs one task", () => {
  compute.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
    HealthCheckPath: "/api/health",
  });
  compute.hasResourceProperties("AWS::ECS::Service", {
    DesiredCount: 1,
    LoadBalancers: Match.arrayWith([Match.objectLike({ ContainerName: "api" })]),
  });
});

function containerDefs(name: string) {
  const defs = compute.findResources("AWS::ECS::TaskDefinition");
  for (const def of Object.values(defs)) {
    const containers = def.Properties.ContainerDefinitions as Array<Record<string, unknown>>;
    const hit = containers.find((c) => c.Name === name);
    if (hit) return hit;
  }
  throw new Error(`no container named ${name}`);
}

function envMap(container: Record<string, unknown>): Record<string, unknown> {
  const env = (container.Environment ?? []) as Array<{ Name: string; Value: unknown }>;
  return Object.fromEntries(env.map((e) => [e.Name, e.Value]));
}

test("only the API task runs migrations; the worker runs the worker module", () => {
  const api = containerDefs("api");
  const worker = containerDefs("worker");
  expect(envMap(api).PTAX_RUN_MIGRATIONS).toBe("1");
  expect(envMap(worker).PTAX_RUN_MIGRATIONS).toBeUndefined();
  expect(worker.Command).toEqual(["python", "-m", "ptax.worker"]);
  expect(api.Command).toBeUndefined();
});

test("both tasks receive database credentials from the cluster secret", () => {
  for (const name of ["api", "worker"]) {
    const container = containerDefs(name);
    const secrets = (container.Secrets ?? []) as Array<{ Name: string }>;
    expect(secrets.map((s) => s.Name)).toEqual(
      expect.arrayContaining(["DATABASE_PASSWORD", "DATABASE_HOST", "DATABASE_USER"]),
    );
    const env = envMap(container);
    expect(env.DATABASE_NAME).toBe("ptax");
    expect(env.S3_BUCKET).toBeDefined();
    expect(env.COGNITO_USER_POOL_ID).toBeDefined();
    expect(env.COGNITO_CLIENT_ID).toBeDefined();
    expect(env.COGNITO_ISSUER).toBeDefined();
    // Local-stack defaults must be explicitly disabled (ECS cannot unset a variable).
    expect(env.S3_ENDPOINT_URL).toBe("");
    expect(env.COGNITO_ENDPOINT_URL).toBe("");
  }
});

test("both tasks use the real NAIP catalog and can read the requester-pays NAIP bucket", () => {
  for (const name of ["api", "worker"]) {
    const env = envMap(containerDefs(name));
    expect(env.NAIP_SOURCE).toBe("stac");
    expect(env.NAIP_AWS_REGION).toBe("us-west-2");
  }
  compute.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: Match.arrayWith(["s3:GetObject", "s3:ListBucket"]),
          Effect: "Allow",
          Resource: Match.arrayWith([
            "arn:aws:s3:::naip-analytic",
            "arn:aws:s3:::naip-analytic/*",
          ]),
        }),
      ]),
    },
  });
});

test("outputs let the operator assemble the ecs run-task command", () => {
  const outputs = compute.findOutputs("*");
  const keys = Object.keys(outputs);
  for (const wanted of ["ClusterName", "WorkerTaskDefinitionArn", "PrivateSubnetIds", "TaskSecurityGroupId", "AlbDnsName"]) {
    expect(keys).toContain(wanted);
  }
});

test("HTTPS listener and redirect appear only when a certificate is supplied", () => {
  compute.resourceCountIs("AWS::ElasticLoadBalancingV2::Listener", 1);
  const withCert = buildStacks(
    new cdk.App({
      context: { certificateArn: "arn:aws:acm:us-east-1:123456789012:certificate/abc" },
    }),
    { imageDirectory: __dirname + "/fixtures/image" },
  );
  const tls = Template.fromStack(withCert.compute);
  tls.resourceCountIs("AWS::ElasticLoadBalancingV2::Listener", 2);
  tls.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", { Port: 443, Protocol: "HTTPS" });
});

test("both services run the one app image, built for Fargate's platform", () => {
  const manifests = app
    .synth()
    .artifacts.filter((a): a is cxapi.AssetManifestArtifact => a instanceof cxapi.AssetManifestArtifact);
  const images = manifests.flatMap((m) => Object.values(m.contents.dockerImages ?? {}));
  expect(images.map((i) => i.source.dockerBuildTarget)).toEqual(["app"]);
  for (const i of images) expect(i.source.platform).toBe("linux/amd64");

  expect(JSON.stringify(containerDefs("api").Image)).toEqual(JSON.stringify(containerDefs("worker").Image));
  for (const name of ["api", "worker"]) {
    expect(envMap(containerDefs(name)).SEGMENTER_MODEL).toBeUndefined();
  }
});

test("the pipeline bucket is KMS-encrypted with a rotating customer key, private and SSL-only", () => {
  data.hasResourceProperties("AWS::KMS::Key", { EnableKeyRotation: true });
  data.hasResourceProperties("AWS::S3::Bucket", {
    BucketEncryption: {
      ServerSideEncryptionConfiguration: [
        Match.objectLike({
          ServerSideEncryptionByDefault: Match.objectLike({
            SSEAlgorithm: "aws:kms",
            KMSMasterKeyID: Match.anyValue(),
          }),
        }),
      ],
    },
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, RestrictPublicBuckets: true },
    VersioningConfiguration: { Status: "Enabled" },
  });
  data.hasResource("AWS::S3::Bucket", {
    Properties: Match.objectLike({ BucketEncryption: Match.anyValue() }),
    DeletionPolicy: "Retain",
  });
});

test("both tasks get the pipeline bucket and may read only per-tenant ArcGIS secrets", () => {
  for (const name of ["api", "worker"]) {
    expect(envMap(containerDefs(name)).PIPELINE_BUCKET).toBeDefined();
  }
  const policies = JSON.stringify(compute.findResources("AWS::IAM::Policy"));
  expect(policies).toContain("kms:Decrypt");
  expect(policies).toContain("secretsmanager:GetSecretValue");
  expect(policies).toContain("secret:ptax/tenants/*");
  expect(policies).not.toMatch(/secretsmanager:GetSecretValue[^\]]*"Resource":"\*"/);
});
