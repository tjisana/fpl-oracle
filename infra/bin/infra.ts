#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { FplOracleStack } from '../lib/fpl-oracle-stack';

const app = new cdk.App();
new FplOracleStack(app, 'FplOracle', {
  // Pin to the account/region the CLI is logged into. The stack uses a
  // scheduled Lambda + S3 bucket, which are region-specific.
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  // Kept out of the repo: `FPL_ORACLE_ALERT_EMAIL=you@example.com npx cdk deploy`.
  alertEmail: process.env.FPL_ORACLE_ALERT_EMAIL,
});
