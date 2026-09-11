import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib/core';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

/**
 * fpl-oracle cloud half, release zero: the daily FPL price snapshot.
 *
 * Why this exists: FPL changes player prices once a night (~01:30 UTC) and
 * exposes no day-level price history, so a snapshot missed is a snapshot
 * gone. The laptop-scheduled job (docs/daily-jobs.md) only fires while the
 * lid is open; this one fires regardless.
 *
 * Shape: EventBridge cron (07:00 UTC daily) -> Lambda (stdlib Python, no
 * deps) -> S3 `snapshots/{YYYY-MM-DD}.json`. The object is byte-compatible
 * with `data/snapshots/*.json` written by `fpl_oracle.fpl.snapshot`, so
 * `scripts/sync_snapshots.sh` can pull the bucket straight into the local
 * tree. Idempotent per UTC date, same as the local job.
 */
const SNAPSHOT_PREFIX = 'snapshots/';

export interface FplOracleStackProps extends cdk.StackProps {
  /**
   * Where failure alarms go. A snapshot missed is unrecoverable, so a
   * failing function must not fail silently. Omit to deploy without an
   * email subscription (the alarm and topic still exist).
   */
  readonly alertEmail?: string;
}

export class FplOracleStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FplOracleStackProps = {}) {
    super(scope, id, props);

    // Durable home for pipeline data (ROADMAP: "durable storage FIRST").
    // RETAIN so a `cdk destroy` or a bad deploy can never delete the price
    // history this whole thing exists to accumulate.
    const dataBucket = new s3.Bucket(this, 'DataBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const snapshotFn = new lambda.Function(this, 'SnapshotFunction', {
      description: 'Daily FPL price snapshot: bootstrap-static -> s3://.../snapshots/{date}.json',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'snapshot'), {
        exclude: ['__pycache__', '*.pyc'],
      }),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        DATA_BUCKET: dataBucket.bucketName,
        SNAPSHOT_PREFIX,
      },
    });
    // Least privilege: read (ListBucket is what lets head_object return a
    // clean 404 instead of 403) plus PutObject on the snapshot prefix only.
    // No delete, ever — the function has no reason to remove history.
    dataBucket.grantRead(snapshotFn);
    snapshotFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [dataBucket.arnForObjects(`${SNAPSHOT_PREFIX}*`)],
      }),
    );

    // 07:00 UTC: after FPL's ~01:30 UTC price change with hours of margin,
    // and the same wall-clock time as the laptop job (3:00 AM EDT). Cron here
    // is UTC-only, so no DST arithmetic — that is the point of the margin.
    new events.Rule(this, 'DailySnapshotRule', {
      description: 'Fire the FPL price snapshot once a day after the nightly price change',
      schedule: events.Schedule.cron({ minute: '0', hour: '7' }),
      targets: [new targets.LambdaFunction(snapshotFn, { retryAttempts: 2 })],
    });

    // Failure alarm. Note the layering: the rule's retryAttempts above
    // retries a failed INVOCATION (Lambda unreachable); this alarm catches a
    // function that ran and raised. Either way, someone is emailed.
    const alerts = new sns.Topic(this, 'AlertTopic', {
      displayName: 'fpl-oracle alerts',
    });
    if (props.alertEmail) {
      alerts.addSubscription(new subscriptions.EmailSubscription(props.alertEmail));
    }
    const snapshotErrors = new cloudwatch.Alarm(this, 'SnapshotErrorsAlarm', {
      alarmDescription: 'The daily FPL price snapshot function raised an error',
      metric: snapshotFn.metricErrors({ period: cdk.Duration.hours(1) }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    snapshotErrors.addAlarmAction(new cwActions.SnsAction(alerts));

    new cdk.CfnOutput(this, 'DataBucketName', {
      value: dataBucket.bucketName,
      description: 'S3 bucket holding snapshots/ (sync with scripts/sync_snapshots.sh)',
    });
    new cdk.CfnOutput(this, 'SnapshotFunctionName', {
      value: snapshotFn.functionName,
      description: 'Invoke by hand: aws lambda invoke --function-name <this> /dev/stdout',
    });
  }
}
