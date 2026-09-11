import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { FplOracleStack } from '../lib/fpl-oracle-stack';

function synth(alertEmail?: string): Template {
  const app = new cdk.App();
  return Template.fromStack(new FplOracleStack(app, 'Test', { alertEmail }));
}

test('the data bucket is private, versioned, and never deleted by the stack', () => {
  const template = synth();
  template.hasResourceProperties('AWS::S3::Bucket', {
    VersioningConfiguration: { Status: 'Enabled' },
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, RestrictPublicBuckets: true },
  });
  template.hasResource('AWS::S3::Bucket', { DeletionPolicy: 'Retain' });
});

test('the snapshot runs once a day at 07:00 UTC, after the nightly price change', () => {
  synth().hasResourceProperties('AWS::Events::Rule', {
    ScheduleExpression: 'cron(0 7 * * ? *)',
    State: 'ENABLED',
  });
});

test('the function knows its bucket and prefix', () => {
  synth().hasResourceProperties('AWS::Lambda::Function', {
    Runtime: 'python3.12',
    Handler: 'handler.handler',
    Environment: {
      Variables: { SNAPSHOT_PREFIX: 'snapshots/', DATA_BUCKET: Match.anyValue() },
    },
  });
});

test('the function can read and write snapshots but never delete anything', () => {
  const policies = synth().findResources('AWS::IAM::Policy');
  const actions = Object.values(policies)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as { Action: string | string[] }[])
    .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
  expect(actions).toContain('s3:PutObject');
  expect(actions.some((a) => a === 's3:ListBucket' || a === 's3:List*')).toBe(true);
  expect(actions.filter((a) => /Delete|Abort/.test(a))).toEqual([]);
});

test('a function error raises an alarm that emails the owner', () => {
  const template = synth('owner@example.com');
  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    MetricName: 'Errors',
    Threshold: 1,
    TreatMissingData: 'notBreaching',
  });
  template.hasResourceProperties('AWS::SNS::Subscription', {
    Protocol: 'email',
    Endpoint: 'owner@example.com',
  });
});

test('without an alert email the alarm still exists, just unsubscribed', () => {
  const template = synth();
  template.resourceCountIs('AWS::CloudWatch::Alarm', 1);
  template.resourceCountIs('AWS::SNS::Subscription', 0);
});
