CREATE TABLE IF NOT EXISTS universe_snapshots (
  snapshot_at timestamptz NOT NULL, rank integer NOT NULL, symbol varchar(6) NOT NULL,
  market varchar(2) NOT NULL, rank_change integer NOT NULL, source text NOT NULL,
  PRIMARY KEY (snapshot_at, symbol)
);
ALTER TABLE universe_snapshots ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE universe_snapshots ADD COLUMN IF NOT EXISTS latest_price double precision;
ALTER TABLE universe_snapshots ADD COLUMN IF NOT EXISTS latest_pct_change double precision;
CREATE TABLE IF NOT EXISTS bars_daily (
  symbol varchar(6) NOT NULL, name text NOT NULL, trade_date date NOT NULL,
  open double precision NOT NULL, close double precision NOT NULL, high double precision NOT NULL,
  low double precision NOT NULL, volume double precision, amount double precision, amplitude double precision,
  pct_change double precision, change double precision, turnover double precision, source text NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);
CREATE TABLE IF NOT EXISTS model_runs (
  run_id uuid PRIMARY KEY, created_at timestamptz NOT NULL, train_end date NOT NULL,
  validation_end date NOT NULL, test_start date NOT NULL, threshold double precision NOT NULL,
  top_k integer NOT NULL, metrics jsonb NOT NULL, feature_names jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
  run_id uuid NOT NULL REFERENCES model_runs(run_id), signal_at timestamptz NOT NULL,
  price_date date,
  symbol varchar(6) NOT NULL, name text NOT NULL, hot_rank integer NOT NULL,
  probability double precision NOT NULL, action text NOT NULL, confidence double precision NOT NULL,
  PRIMARY KEY (run_id, symbol)
);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS price_date date;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_price double precision;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_pct_change double precision;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS raw_action text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb;
-- Migrate the legacy "distance from 50%" scale to directional probability once.
UPDATE signals SET confidence=0.5+confidence/2 WHERE confidence<0.5;
UPDATE signals SET action=raw_action WHERE action='NO_SIGNAL' AND raw_action IN ('BUY','HOLD','SELL');
CREATE TABLE IF NOT EXISTS signal_evaluations (
  run_id uuid NOT NULL REFERENCES model_runs(run_id) ON DELETE CASCADE,
  symbol varchar(6) NOT NULL,
  signal_date date NOT NULL,
  evaluation_date date NOT NULL,
  signal_action text NOT NULL,
  raw_action text NOT NULL,
  signal_price double precision NOT NULL,
  next_open double precision NOT NULL,
  next_close double precision NOT NULL,
  close_to_close_return double precision NOT NULL,
  executable_return double precision NOT NULL,
  direction_correct boolean NOT NULL,
  executable_correct boolean NOT NULL,
  evaluated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, symbol)
);
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS signal_median_price double precision;
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS evaluation_median_price double precision;
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS median_return double precision;
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS median_correct boolean;
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS evaluation_method text NOT NULL DEFAULT 'OHLC_MEDIAN_PROXY';
CREATE INDEX IF NOT EXISTS idx_signal_eval_date ON signal_evaluations(evaluation_date DESC);
CREATE INDEX IF NOT EXISTS idx_bars_daily_date ON bars_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(signal_at DESC);

CREATE TABLE IF NOT EXISTS analysis_universe (
  symbol varchar(6) PRIMARY KEY,
  included boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE analysis_universe ADD COLUMN IF NOT EXISTS purchase_price double precision;
ALTER TABLE analysis_universe ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT true;
ALTER TABLE analysis_universe ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual';
DO $$ BEGIN
  ALTER TABLE analysis_universe ADD CONSTRAINT analysis_universe_purchase_price_positive
    CHECK (purchase_price IS NULL OR purchase_price > 0) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS stock_name_overrides (
  symbol varchar(6) PRIMARY KEY,
  name_cn text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO stock_name_overrides(symbol,name_cn) VALUES
  ('300285',U&'\56FD\74F7\6750\6599'),('600176',U&'\4E2D\56FD\5DE8\77F3'),
  ('600206',U&'\6709\7814\65B0\6750'),('601899',U&'\7D2B\91D1\77FF\4E1A'),
  ('002197',U&'\8BC1\901A\7535\5B50'),('603629',U&'\5229\901A\7535\5B50'),
  ('002409',U&'\96C5\514B\79D1\6280'),('002281',U&'\5149\8FC5\79D1\6280'),
  ('603123',U&'\7FE0\5FAE\80A1\4EFD')
ON CONFLICT(symbol) DO UPDATE SET name_cn=excluded.name_cn,updated_at=now();

CREATE TABLE IF NOT EXISTS backtest_equity (
  run_id uuid NOT NULL REFERENCES model_runs(run_id) ON DELETE CASCADE,
  trade_date date NOT NULL,
  strategy_return double precision NOT NULL,
  equity double precision NOT NULL,
  PRIMARY KEY (run_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_backtest_equity_date ON backtest_equity(trade_date);

CREATE TABLE IF NOT EXISTS model_feature_weights (
  run_id uuid NOT NULL REFERENCES model_runs(run_id) ON DELETE CASCADE,
  feature_name text NOT NULL,
  coefficient double precision NOT NULL,
  PRIMARY KEY (run_id, feature_name)
);

CREATE TABLE IF NOT EXISTS signal_changes (
  id bigserial PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES model_runs(run_id) ON DELETE CASCADE,
  previous_run_id uuid REFERENCES model_runs(run_id) ON DELETE SET NULL,
  changed_at timestamptz NOT NULL,
  symbol varchar(6) NOT NULL,
  previous_action text,
  current_action text,
  previous_probability double precision,
  current_probability double precision,
  change_type text NOT NULL,
  UNIQUE (run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_signal_changes_time ON signal_changes(changed_at DESC);

CREATE TABLE IF NOT EXISTS data_quality_snapshots (
  run_id uuid PRIMARY KEY REFERENCES model_runs(run_id) ON DELETE CASCADE,
  checked_at timestamptz NOT NULL,
  symbol_count integer NOT NULL,
  bar_count bigint NOT NULL,
  earliest_date date,
  latest_date date,
  stale_symbol_count integer NOT NULL,
  download_error_count integer NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE VIEW v_latest_model_run AS
SELECT * FROM model_runs ORDER BY created_at DESC LIMIT 1;

CREATE OR REPLACE VIEW v_latest_signals AS
SELECT s.run_id,s.signal_at,s.symbol,s.name,s.hot_rank,s.probability,s.action,s.confidence,s.price_date,
       u.latest_price,u.latest_pct_change,u.rank_change,
       s.signal_price,s.signal_pct_change,s.raw_action,s.risk_flags
FROM signals s
LEFT JOIN LATERAL (
  SELECT us.latest_price, us.latest_pct_change, us.rank_change
  FROM universe_snapshots us
  WHERE us.symbol = s.symbol AND us.snapshot_at <= s.signal_at
  ORDER BY us.snapshot_at DESC LIMIT 1
) u ON true
WHERE s.run_id = (SELECT run_id FROM v_latest_model_run);

CREATE TABLE IF NOT EXISTS research_ingestion_runs (
  run_id uuid PRIMARY KEY, started_at timestamptz NOT NULL, completed_at timestamptz,
  status text NOT NULL, source text NOT NULL, fetched_count integer NOT NULL DEFAULT 0,
  saved_count integer NOT NULL DEFAULT 0, analyzed_count integer NOT NULL DEFAULT 0, error_message text
);
CREATE TABLE IF NOT EXISTS industry_research_reports (
  report_id text PRIMARY KEY, title text NOT NULL, industry_code text, industry_name text NOT NULL,
  broker_code text, broker_name text NOT NULL, rating text, rating_change integer, researcher text,
  publish_date date NOT NULL, source_url text NOT NULL, source text NOT NULL,
  fetched_at timestamptz NOT NULL, raw_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE industry_research_reports ADD COLUMN IF NOT EXISTS industry_key text;
ALTER TABLE industry_research_reports ADD COLUMN IF NOT EXISTS rating_category text;
CREATE TABLE IF NOT EXISTS stock_master (
  symbol varchar(6) PRIMARY KEY,
  name_cn text NOT NULL,
  industry text,
  industry_key text,
  updated_at timestamptz NOT NULL,
  source text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_master_industry_key ON stock_master(industry_key);
CREATE INDEX IF NOT EXISTS idx_research_publish_date ON industry_research_reports(publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_research_industry ON industry_research_reports(industry_name, publish_date DESC);
CREATE TABLE IF NOT EXISTS industry_research_analysis (
  report_id text PRIMARY KEY REFERENCES industry_research_reports(report_id) ON DELETE CASCADE,
  analyzed_at timestamptz NOT NULL, sentiment_score double precision NOT NULL, stance text NOT NULL,
  themes jsonb NOT NULL DEFAULT '[]'::jsonb, catalysts jsonb NOT NULL DEFAULT '[]'::jsonb,
  risks jsonb NOT NULL DEFAULT '[]'::jsonb, summary text NOT NULL, model_version text NOT NULL
);
CREATE TABLE IF NOT EXISTS industry_daily_digest (
  digest_date date NOT NULL, industry_name text NOT NULL, report_count integer NOT NULL,
  broker_count integer NOT NULL, sentiment_score double precision NOT NULL, stance text NOT NULL,
  themes jsonb NOT NULL DEFAULT '[]'::jsonb, summary text NOT NULL,
  representative_reports jsonb NOT NULL DEFAULT '[]'::jsonb, updated_at timestamptz NOT NULL,
  PRIMARY KEY (digest_date, industry_name)
);

CREATE TABLE IF NOT EXISTS global_market_assets (
  symbol text PRIMARY KEY, name text NOT NULL, category text NOT NULL, country text,
  currency text, timezone text, source text NOT NULL, updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS global_market_bars (
  symbol text NOT NULL REFERENCES global_market_assets(symbol), interval text NOT NULL,
  bar_time timestamptz NOT NULL, open double precision, high double precision, low double precision,
  close double precision NOT NULL, volume double precision, source text NOT NULL,
  PRIMARY KEY(symbol,interval,bar_time)
);
CREATE INDEX IF NOT EXISTS idx_global_bars_time ON global_market_bars(bar_time DESC);
CREATE TABLE IF NOT EXISTS global_market_snapshots (
  symbol text NOT NULL REFERENCES global_market_assets(symbol), observed_at timestamptz NOT NULL,
  price double precision NOT NULL, previous_close double precision, change double precision,
  change_pct double precision, market_state text NOT NULL, source text NOT NULL,
  PRIMARY KEY(symbol,observed_at)
);
CREATE INDEX IF NOT EXISTS idx_global_snapshot_time ON global_market_snapshots(observed_at DESC);
CREATE TABLE IF NOT EXISTS macro_series (
  series_code text NOT NULL, country text NOT NULL, name text NOT NULL, category text NOT NULL,
  observation_date date NOT NULL, value double precision NOT NULL, unit text, source text NOT NULL,
  PRIMARY KEY(series_code,country,observation_date)
);
CREATE TABLE IF NOT EXISTS trade_flows (
  period integer NOT NULL, reporter_code text NOT NULL, reporter_name text NOT NULL,
  flow_code text NOT NULL, hs_code text NOT NULL, hs_name text NOT NULL,
  value_usd double precision NOT NULL, source text NOT NULL,
  PRIMARY KEY(period,reporter_code,flow_code,hs_code)
);
CREATE TABLE IF NOT EXISTS trade_opportunities (
  as_of date NOT NULL, reporter_name text NOT NULL, hs_code text NOT NULL, hs_name text NOT NULL,
  flow_code text NOT NULL, latest_value_usd double precision NOT NULL, yoy_growth double precision,
  opportunity_score double precision NOT NULL, beneficiary_industries jsonb NOT NULL,
  risk_note text NOT NULL, PRIMARY KEY(as_of,reporter_name,hs_code,flow_code)
);
CREATE TABLE IF NOT EXISTS cycle_assessments (
  as_of date NOT NULL, model_name text NOT NULL, phase text NOT NULL, score double precision NOT NULL,
  confidence double precision NOT NULL, evidence jsonb NOT NULL, guidance jsonb NOT NULL,
  PRIMARY KEY(as_of,model_name)
);
CREATE TABLE IF NOT EXISTS fund_flow_snapshots (
  observed_at timestamptz NOT NULL, symbol varchar(6) NOT NULL, name text NOT NULL,
  price double precision, pct_change double precision, amount double precision,
  main_net double precision, main_ratio double precision, super_net double precision, super_ratio double precision,
  large_net double precision, large_ratio double precision, medium_net double precision, medium_ratio double precision,
  small_net double precision, small_ratio double precision, source text NOT NULL,
  PRIMARY KEY(observed_at,symbol)
);
CREATE INDEX IF NOT EXISTS idx_fund_flow_symbol_time ON fund_flow_snapshots(symbol,observed_at DESC);
CREATE TABLE IF NOT EXISTS fund_flow_scores (
  as_of timestamptz NOT NULL, symbol varchar(6) NOT NULL, name text NOT NULL,
  follow_score double precision NOT NULL, reliability double precision NOT NULL,
  recommendation text NOT NULL, factors jsonb NOT NULL, PRIMARY KEY(as_of,symbol)
);
CREATE TABLE IF NOT EXISTS daily_briefings (
  brief_date date PRIMARY KEY, generated_at timestamptz NOT NULL, subject text NOT NULL,
  body_html text NOT NULL, metrics jsonb NOT NULL, recipient text, email_status text NOT NULL,
  sent_at timestamptz, error_message text
);
