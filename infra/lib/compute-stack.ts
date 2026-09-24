import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecsPatterns from "aws-cdk-lib/aws-ecs-patterns";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { DATABASE_NAME } from "./data-stack";

export interface ComputeStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  cluster: rds.DatabaseCluster;
  appSecurityGroup: ec2.ISecurityGroup;
  uploadsBucket: s3.IBucket;
  pipelineBucket: s3.IBucket;
  pipelineKey: kms.IKey;
  userPool: cognito.IUserPool;
  userPoolClient: cognito.IUserPoolClient;
  /** Docker build context; defaults to the repository root (backend/Dockerfile). */
  imageDirectory?: string;
  /** ACM certificate ARN for HTTPS on the ALB; HTTP-only when omitted. */
  certificateArn?: string;
}

export class ComputeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const ecsCluster = new ecs.Cluster(this, "Cluster", { vpc: props.vpc });

    // One image for both services: the API serves it, the worker overrides the command.
    const image = ecs.ContainerImage.fromDockerImageAsset(
      new ecrAssets.DockerImageAsset(this, "Image", {
        directory: props.imageDirectory ?? `${__dirname}/../..`,
        file: "backend/Dockerfile",
        target: "app",
        platform: ecrAssets.Platform.LINUX_AMD64,
      }),
    );

    const secret = props.cluster.secret!;
    const issuer = `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}`;
    const environment = {
      DATABASE_NAME,
      DATABASE_PORT: "5432",
      S3_BUCKET: props.uploadsBucket.bucketName,
      PIPELINE_BUCKET: props.pipelineBucket.bucketName,
      AWS_REGION: this.region,
      COGNITO_USER_POOL_ID: props.userPool.userPoolId,
      COGNITO_CLIENT_ID: props.userPoolClient.userPoolClientId,
      COGNITO_ISSUER: issuer,
      STATIC_DIR: "/app/static",
      // Real NAIP discovery/ingest (the local default is the committed fixture source).
      NAIP_SOURCE: "stac",
      NAIP_AWS_REGION: "us-west-2",
      // "" disables the local MinIO / cognito-local defaults (Settings treats "" as unset).
      S3_ENDPOINT_URL: "",
      S3_ACCESS_KEY_ID: "",
      S3_SECRET_ACCESS_KEY: "",
      COGNITO_ENDPOINT_URL: "",
    };
    // The Aurora secret has no URL field; Settings composes DATABASE_URL from these parts.
    const secrets = () => ({
      DATABASE_HOST: ecs.Secret.fromSecretsManager(secret, "host"),
      DATABASE_USER: ecs.Secret.fromSecretsManager(secret, "username"),
      DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(secret, "password"),
    });

    const taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });
    props.uploadsBucket.grantReadWrite(taskRole);
    props.pipelineBucket.grantReadWrite(taskRole);
    props.pipelineKey.grantEncryptDecrypt(taskRole);
    // Each county's ArcGIS OAuth app credentials, named `ptax/tenants/<tenant_id>/arcgis`.
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:ptax/tenants/*`],
      }),
    );
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminSetUserPassword",
          "cognito-idp:AdminDeleteUser",
          "cognito-idp:AdminGetUser",
        ],
        resources: [props.userPool.userPoolArn],
      }),
    );
    // NAIP on AWS is a requester-pays bucket in us-west-2; the worker clips tiles from it.
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:ListBucket"],
        resources: ["arn:aws:s3:::naip-analytic", "arn:aws:s3:::naip-analytic/*"],
      }),
    );

    const certificate = props.certificateArn
      ? acm.Certificate.fromCertificateArn(this, "Certificate", props.certificateArn)
      : undefined;

    // Imported as immutable so nothing in this stack writes rules back into the data
    // stack's group (that would be a cross-stack cycle); it already allows the DB port.
    const appSg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      "AppSg",
      props.appSecurityGroup.securityGroupId,
      { mutable: false },
    );
    // ALB -> API ingress lives on a group this stack owns.
    const apiSg = new ec2.SecurityGroup(this, "ApiSg", {
      vpc: props.vpc,
      description: "ptax API tasks (ALB ingress)",
    });

    // SHORTCUT: migrations run at API container start (advisory-locked, worker excluded) with a
    // single API task. Upgrade trigger: scaling the API beyond one task, at which point migrations
    // move to a one-off ECS task run before the service update.
    const api = new ecsPatterns.ApplicationLoadBalancedFargateService(this, "Api", {
      cluster: ecsCluster,
      desiredCount: 1,
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      cpu: 1024,
      memoryLimitMiB: 2048,
      publicLoadBalancer: true,
      certificate,
      redirectHTTP: certificate !== undefined,
      securityGroups: [apiSg, appSg],
      taskImageOptions: {
        image,
        containerName: "api",
        containerPort: 8000,
        taskRole,
        environment: { ...environment, PTAX_RUN_MIGRATIONS: "1" },
        secrets: secrets(),
        logDriver: ecs.LogDrivers.awsLogs({
          streamPrefix: "api",
          logRetention: logs.RetentionDays.ONE_MONTH,
        }),
      },
    });
    api.targetGroup.configureHealthCheck({ path: "/api/health", healthyHttpCodes: "200" });

    const workerTask = new ecs.FargateTaskDefinition(this, "WorkerTask", {
      cpu: 2048,
      memoryLimitMiB: 4096,
      taskRole,
    });
    workerTask.addContainer("worker", {
      image,
      command: ["python", "-m", "ptax.worker"],
      environment,
      secrets: secrets(),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "worker",
        logRetention: logs.RetentionDays.ONE_MONTH,
      }),
    });
    new ecs.FargateService(this, "Worker", {
      cluster: ecsCluster,
      taskDefinition: workerTask,
      desiredCount: 1,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
    });

    // Everything the operator needs to run ptax-admin as a one-off task inside the VPC.
    new cdk.CfnOutput(this, "AlbDnsName", { value: api.loadBalancer.loadBalancerDnsName });
    new cdk.CfnOutput(this, "ClusterName", { value: ecsCluster.clusterName });
    new cdk.CfnOutput(this, "WorkerTaskDefinitionArn", { value: workerTask.taskDefinitionArn });
    new cdk.CfnOutput(this, "PrivateSubnetIds", {
      value: props.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds.join(","),
    });
    new cdk.CfnOutput(this, "TaskSecurityGroupId", {
      value: props.appSecurityGroup.securityGroupId,
    });
  }
}
