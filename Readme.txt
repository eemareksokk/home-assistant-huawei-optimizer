# Battery Optimizer

## Description
A battery optimization system designed to maximize economic benefits from a home battery storage system by intelligently managing battery charging and discharging based on:
- Nordpool electricity prices
- Solar PV production forecast
- Battery state and constraints
- Home energy consumption patterns

## Key Features
- Optimizes battery operations using linear programming (LP)
- Supports multiple operating modes:
  * Time of Use (TOU) charging during low-price periods
  * Feed to grid during high-price periods
  * Maximum self-consumption mode
  * TOU none mode for low activity periods
- Real-time adaptation to:
  * Current electricity prices from Nordpool
  * Solar production forecasts
  * Battery state of charge
- Automatic scheduling (runs hourly)
- Manual trigger option via Home Assistant button

## Technical Specifications
- Battery Parameters:
  * Total Capacity: 15.0 kWh
  * Maximum Charge/Discharge Rate: 4.4 kWh/hour
  * Charging Efficiency: 95%
  * Minimum State of Charge: 0,75 kWh 
- Grid Parameters:
  * Maximum Grid Load: 4400W
  * Minimum Grid Load: 500W (during negative grid price)
  * Grid Fee: 9.00 € cents
  * Minimum Grid Sell Price: 5.00 € cents
  * Minimum Grid Sell Amount: 1 kWh

## Dependencies
- Home Assistant with AppDaemon
- Python packages:
  * pulp (for linear programming optimization)
  * appdaemon.plugins.hass.hassapi

## Integration Requirements
- Huawei Solar/Battery system
- Nordpool price sensor
- Solcast PV forecast integration
- Home Assistant sensors:
  * Battery state of capacity
  * Nordpool kWh prices
  * Solcast PV forecast

## Operating Logic
The system optimizes battery operation by:
1. Collecting current electricity prices and forecasts
2. Analyzing PV production forecasts
3. Considering self-consumption patterns (varies by month)
4. Running optimization with and without grid charging
5. Selecting the most profitable operating strategy
6. Automatically adjusting battery working mode and grid feed parameters

The optimization aims to maximize profits through:
- Strategic grid energy purchases during low-price periods
- Optimal solar energy utilization
- Efficient self-consumption management
- Strategic grid energy sales during high-price periods

## Automatic Scheduling
- Runs hourly at the start of each hour
- Can be manually triggered via Home Assistant button

## Economic Considerations
- Implements minimum profit thresholds for grid charging
- Considers grid fees and costs in optimization
- Adapts to monthly self-consumption patterns
- Ensures minimum profitability for battery grid charging operations
