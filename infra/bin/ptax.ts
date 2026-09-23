#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { buildStacks } from "../lib/app";

const app = new cdk.App();
buildStacks(app);
