import * as cdk from "aws-cdk-lib";
import { AuthStack } from "./auth-stack";
import { ComputeStack } from "./compute-stack";
import { DataStack } from "./data-stack";
import { NetworkStack } from "./network-stack";

export interface BuildOptions {
  imageDirectory?: string;
}

/** Wire the four stacks; shared by bin/ptax.ts and the assertion tests. */
export function buildStacks(app: cdk.App, options: BuildOptions = {}) {
  const env = {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  };
  const certificateArn = app.node.tryGetContext("certificateArn") as string | undefined;

  const network = new NetworkStack(app, "PtaxNetwork", { env });
  const data = new DataStack(app, "PtaxData", { env, vpc: network.vpc });
  const auth = new AuthStack(app, "PtaxAuth", { env });
  const compute = new ComputeStack(app, "PtaxCompute", {
    env,
    vpc: network.vpc,
    cluster: data.cluster,
    appSecurityGroup: data.appSecurityGroup,
    uploadsBucket: data.uploadsBucket,
    pipelineBucket: data.pipelineBucket,
    pipelineKey: data.pipelineKey,
    userPool: auth.userPool,
    userPoolClient: auth.userPoolClient,
    imageDirectory: options.imageDirectory,
    certificateArn,
  });
  return { network, data, auth, compute };
}
