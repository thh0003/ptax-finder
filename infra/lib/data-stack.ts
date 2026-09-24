import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import * as rds from "aws-cdk-lib/aws-rds";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

export interface DataStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
}

export const DATABASE_NAME = "ptax";

export class DataStack extends cdk.Stack {
  readonly cluster: rds.DatabaseCluster;
  readonly uploadsBucket: s3.Bucket;
  /** Parcel improvement pipeline storage: per-tenant prefixes `tenants/<tenant_id>/`. */
  readonly pipelineBucket: s3.Bucket;
  readonly pipelineKey: kms.Key;
  /** Security group for anything that may reach the database (API, worker, one-off tasks). */
  readonly appSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    // Defined here (not in the compute stack) so granting DB access never creates a
    // cross-stack cycle: compute only *uses* this group.
    this.appSecurityGroup = new ec2.SecurityGroup(this, "AppSg", {
      vpc: props.vpc,
      description: "ptax application tasks",
    });

    // Aurora Serverless v2 PostgreSQL 16; min 0 ACU lets it pause when idle.
    this.cluster = new rds.DatabaseCluster(this, "Database", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        // Check `aws rds describe-db-engine-versions --engine aurora-postgresql` when bumping;
        // older 16.x minors get withdrawn (16.6 was rejected in us-east-1).
        version: rds.AuroraPostgresEngineVersion.VER_16_13,
      }),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      writer: rds.ClusterInstance.serverlessV2("writer"),
      serverlessV2MinCapacity: 0,
      serverlessV2MaxCapacity: 4,
      defaultDatabaseName: DATABASE_NAME,
      credentials: rds.Credentials.fromGeneratedSecret("ptax"),
      storageEncrypted: true,
      removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
    });
    this.cluster.connections.allowDefaultPortFrom(this.appSecurityGroup, "application tasks");

    // Browser-direct presigned PUTs; the key is unguessable so any origin may PUT to it.
    this.uploadsBucket = new s3.Bucket(this, "Uploads", {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      cors: [
        {
          allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
          allowedOrigins: ["*"],
          allowedHeaders: ["*"],
          exposedHeaders: ["ETag"],
          maxAge: 3600,
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // County imagery, parcels and results for the detection pipeline, encrypted with a
    // customer-managed key. No CORS: nothing uploads to it from a browser.
    this.pipelineKey = new kms.Key(this, "PipelineKey", {
      description: "ptax parcel improvement pipeline data",
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.pipelineBucket = new s3.Bucket(this, "Pipeline", {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.pipelineKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
  }
}
