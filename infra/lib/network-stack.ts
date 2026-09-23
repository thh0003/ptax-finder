import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

export class NetworkStack extends cdk.Stack {
  readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    // Two AZs for the ALB and Aurora; one NAT gateway keeps the bill small.
    this.vpc = new ec2.Vpc(this, "Vpc", { maxAzs: 2, natGateways: 1 });
  }
}
