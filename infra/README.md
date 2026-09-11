# infra

AWS CDK app (TypeScript) for fpl-oracle's cloud half. What it deploys, why,
and the day-to-day commands are documented in `../docs/daily-jobs.md`
("Cloud snapshot"). Requires an `aws login` session.

```sh
npm test          # stack assertions
npx cdk diff      # preview
npx cdk deploy    # apply
```
