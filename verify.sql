
SELECT table_name, column_name, data_type 
FROM information_schema.columns 
WHERE table_schema = 'public' 
AND (
    (table_name = 'monitored_positions' AND column_name IN ('stop_order_id', 'strategy', 'run_id', 'entry_context')) OR
    (table_name = 'agent_positions' AND column_name = 'side') OR
    (table_name = 'trades' AND column_name IN ('run_id', 'strategy'))
);

