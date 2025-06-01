import datetime
from pulp import LpProblem, LpMaximize, LpVariable, lpSum, LpStatus, LpBinary
import appdaemon.plugins.hass.hassapi as hass


class BatteryOptimizer(hass.Hass):
    # Battery and system parameters
    BATTERY_CAPACITY = 15.0  # total battery capacity [kWh]
    MAX_CHARGE = 4.4  # maximum battery charging rate [kWh/hour]
    MAX_DISCHARGE = 4.4  # maximum battery discharge rate [kWh/hour]
    EFFICIENCY = 0.95  # Battery charging efficiency [%]
    MINIMAL_SOC = 0.75  # Minimum battery state of charge [kWh]
    GRID_FEE = 9.00  # grid fee i.e. cost for electricity purchase [€ cents]
    GRID_BUY_COST = 0.45  # additional cost for electricity purchase [€ cents]
    GRID_SELL_COST = 0.8  # additional cost for electricity sale [€ cents]
    MIN_GRID_SELL_PRICE = 5.00  # minimum price for selling from battery to grid [€ cents]
    MIN_GRID_SELL_AMOUNT = 1  # minimum amount for selling from battery to grid [kWh]
    MIN_CYCLE_AMOUNT = 0.5  # minimum amount for grid charging and grid selling for determining inverter_mode [kWh]
    MIN_BATTERY_GRID_CHARGE_PROFIT = 50  # minimum profit to use grid charging [€ cents]
    BATTERY_GRID_SHARE_PERCENT = 0.66  # Share of battery self-consumption and grid imports caused by phase load imbalance [%]
    BATTERY_DEVICE_ID = "03155398ac6dbd812ccbfde86517bd24"  # check from Home Assistant
    INVERTER_DEVICE_ID = "97197cc18141687a9c461766b7dbef4a"  # check from Home Assistant
    MAX_GRID_LOAD = 4400 # max grid load on normal condition [W]
    MIN_GRID_LOAD = 500 # max grid load during negative grid price [W]

    def initialize(self):
        # Run every full hour
        self.run_hourly(self.optimize_battery, datetime.time(0, 0, 0))
        # Run when button is pressed 
        self.listen_event(self.optimize_battery, "call_service", domain="input_button", service="press", entity_id="input_button.my_button")

    def optimize_battery(self, *args, **kwargs):
        now = datetime.datetime.now()
        self.current_time = now.hour

        # Set self_consumption according to calendar month
        self_consumption_by_month = {
            1: 1.075, 2: 1.075, 3: 0.85, 4: 0.8,
            5: 0.65, 6: 0.55, 7: 0.55, 8: 0.55,
            9: 0.65, 10: 0.8, 11: 0.95, 12: 1.075
        }
        self.self_consumption = self_consumption_by_month.get(now.month, 0.62)

        # Nordpool price information
        price_entity = "sensor.nordpool_kwh_ee_eur_3_10_0"
        state = self.get_state(price_entity, attribute="all")
        if not state or "attributes" not in state:
            self.log("Failed to read sensor data.", level="WARNING")
            return

        attrs = state["attributes"]
        today_prices = list(map(float, attrs.get("today", [0.0] * 24)))
        self.tomorrow_valid = attrs.get("tomorrow_valid", False)
        tomorrow_prices = list(map(float, attrs.get("tomorrow", [0.0] * 24))) if self.tomorrow_valid else [0.0] * 24
        self.prices = today_prices + tomorrow_prices

        # Set n according to tomorrow_valid value
        self.n = 48 if self.tomorrow_valid else 24

        # PV forecast – today
        forecast_today_entity = "sensor.solcast_pv_forecast_forecast_today"
        pv_today_data = self.get_state(forecast_today_entity, attribute="detailedHourly")
        pv_today = [round(item.get("pv_estimate", 0), 4) for item in pv_today_data] if pv_today_data else [0.0] * 24

        # PV forecast – tomorrow
        forecast_tomorrow_entity = "sensor.solcast_pv_forecast_forecast_tomorrow"
        pv_tomorrow_data = self.get_state(forecast_tomorrow_entity, attribute="detailedHourly")
        pv_tomorrow = [round(item.get("pv_estimate", 0), 4) for item in pv_tomorrow_data] if pv_tomorrow_data else [0.0] * 24

        self.pv_forecast = pv_today + pv_tomorrow

        # Read battery remaining energy amount
        try:
            soc_percent = float(self.get_state("sensor.batteries_state_of_capacity"))
        except (TypeError, ValueError):
            self.log("Invalid battery SOC value", level="ERROR")
            return

        self.initial_soc = round((soc_percent / 100) * self.BATTERY_CAPACITY, 2)

        # Start the optimizer
        first_run = self.solve_optimization(allow_grid_charging=True)
        second_run = self.solve_optimization(allow_grid_charging=False) 

        profit_difference = first_run['profit'] - second_run['profit']

        self.log(f"first_run['profit']: {first_run['profit']}")
        self.log(f"second_run['profit']: {second_run['profit']}")
        self.log(f"Profit difference: {profit_difference}")

        # If the effect of battery grid charging is less than min. profit, then don't use grid charging
        if profit_difference < self.MIN_BATTERY_GRID_CHARGE_PROFIT:
            first_run = second_run

        current_working_mode = first_run['inverter_mode'][self.current_time]

        # Update sensor information
        self.set_state("sensor.energy_planning_sensor", state="Ok", attributes={
            "price": self.prices,
            "pv_forecast": self.pv_forecast,
            "grid_buy": first_run['grid_buy'],
            "grid_sell": first_run['grid_sell'],
            "self_used": first_run['self_used'],
            "solar_self_used": first_run['solar_self_used'],
            "solar_to_battery": first_run['solar_to_battery'],
            "solar_to_grid": first_run['solar_to_grid'],
            "soc": first_run['soc'],
            "inverter_mode": first_run['inverter_mode'],
            "current_working_mode": current_working_mode
        })

        # Set feed grid power based on current price 
        if self.prices[self.current_time] < self.GRID_SELL_COST:
            self.call_service("huawei_solar/set_maximum_feed_grid_power", power=self.MIN_GRID_LOAD, device_id=self.INVERTER_DEVICE_ID)
        else:
            self.call_service("huawei_solar/set_maximum_feed_grid_power", power=self.MAX_GRID_LOAD, device_id=self.INVERTER_DEVICE_ID)

        # Change battery operating mode
        if current_working_mode == 'TOU none':
            # Use TOU period "00:00-00:01/1/-"
            self.call_service("huawei_solar/set_tou_periods", device_id=self.BATTERY_DEVICE_ID, periods="00:00-00:01/1/-")
            self.call_service("select/select_option", entity_id="select.batteries_working_mode", option="time_of_use_luna2000")
        elif current_working_mode == 'feed to grid':
            self.call_service("select/select_option", entity_id="select.batteries_working_mode", option="fully_fed_to_grid")
            self.call_service("huawei_solar/set_maximum_feed_grid_power", power=first_run['grid_sell'][self.current_time] * 1000, device_id=self.INVERTER_DEVICE_ID)
        elif current_working_mode == 'TOU charge':
            start_time = now.replace(minute=0, second=0, microsecond=0)
            end_time = start_time + datetime.timedelta(hours=1)
            weekday_number = now.weekday() + 1
            # Create TOU period in the format "HH:MM-HH:MM/weekday_number/+"
            period_string = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}/{weekday_number}/+"
            self.call_service("huawei_solar/set_tou_periods", device_id=self.BATTERY_DEVICE_ID, periods=period_string)
            self.call_service("select/select_option", entity_id="select.batteries_working_mode", option="time_of_use_luna2000")
        else: # default mode "max self consumption"
            self.call_service("select/select_option", entity_id="select.batteries_working_mode", option="maximise_self_consumption")

    def solve_optimization(self, allow_grid_charging=True):
        """
        Solves the battery optimization problem using linear programming.
        """
        prob = LpProblem("Battery_Optimization", LpMaximize)
        vars = self._define_variables()
        self._add_constraints(prob, vars, allow_grid_charging)
        self._set_objective(prob, vars)
        prob.solve()
        return self._extract_results(prob, vars)

    def _define_variables(self):
        # Define all LP variables
        d = {}
        soc = {}
        u = {}
        self_used = {}
        solar_self_used = {}
        solar_to_battery = {}
        solar_to_grid = {}
        grid_buy = {}
        grid_sell = {}
        grid_sell_binary = {}
        for i in range(self.current_time, self.n):
            d[i] = LpVariable(f"discharge_{i}", 0, self.MAX_DISCHARGE)  # Total discharge
            soc[i] = LpVariable(f"soc_{i}", 0, self.BATTERY_CAPACITY)
            u[i] = LpVariable(f"use_{i}", 0, 1, cat=LpBinary)  # Whether to discharge or not
            grid_sell_binary[i] = LpVariable(f"grid_sell_binary_{i}", 0, 1, cat=LpBinary)  # Whether to sell to grid or not
            self_used[i] = LpVariable(f"self_used_{i}", 0, min(self.self_consumption, self.MAX_DISCHARGE)) # How much energy is used for self-consumption (limited by self-consumption amount)
            solar_self_used[i] = LpVariable(f"solar_self_used_{i}", 0, min(self.self_consumption, self.pv_forecast[i])) # How much solar energy is used directly for self-consumption
            solar_to_battery[i] = LpVariable(f"solar_to_battery_{i}", 0, min(self.MAX_CHARGE, self.pv_forecast[i])) # How much solar energy is used for battery charging
            solar_to_grid[i] = LpVariable(f"solar_to_grid_{i}", 0, self.pv_forecast[i]) # How much solar energy is left over (selling to grid)
            grid_buy[i] = LpVariable(f"grid_buy_{i}", 0, self.MAX_CHARGE)  # Buying from grid
            grid_sell[i] = LpVariable(f"grid_sell_{i}", 0, self.MAX_DISCHARGE)  # Selling to grid
        return {
            'd': d,
            'soc': soc,
            'u': u,
            'self_used': self_used,
            'solar_self_used': solar_self_used,
            'solar_to_battery': solar_to_battery,
            'solar_to_grid': solar_to_grid,
            'grid_buy': grid_buy,
            'grid_sell': grid_sell,
            'grid_sell_binary': grid_sell_binary
        }

    def _add_constraints(self, prob, vars, allow_grid_charging):
        # Apply all LP constraints
        d = vars['d']
        soc = vars['soc']
        u = vars['u']
        self_used = vars['self_used']
        solar_self_used = vars['solar_self_used']
        solar_to_battery = vars['solar_to_battery']
        solar_to_grid = vars['solar_to_grid']
        grid_buy = vars['grid_buy']
        grid_sell = vars['grid_sell']
        grid_sell_binary = vars['grid_sell_binary']

        epsilon = 0.001
        first_hour = self.current_time
        # Battery dynamics (considering solar energy and efficiency)
        # First hour uses the initial state of charge (initial_soc)        
        prob += soc[first_hour] == self.initial_soc + self.EFFICIENCY * solar_to_battery[first_hour] + self.EFFICIENCY * grid_buy[first_hour] - d[first_hour]

        # Subsequent hours use the state of charge from the previous hour
        for i in range(self.current_time + 1, self.n):
            prev_hour = i - 1
            prob += soc[i] == soc[prev_hour] + self.EFFICIENCY * solar_to_battery[i] + self.EFFICIENCY * grid_buy[i] - d[i]

        for i in range(self.current_time, self.n):
            # All solar production must be used: either for self-use, battery charge or grid feed-in
            prob += solar_self_used[i] + solar_to_battery[i] + solar_to_grid[i] == self.pv_forecast[i]
            # Enforce minimum state of charge
            prob += soc[i] >= self.MINIMAL_SOC
            # Non-negativity constraints
            prob += solar_self_used[i] >= 0
            prob += solar_to_battery[i] >= 0
            prob += solar_to_grid[i] >= 0
            prob += grid_buy[i] >= 0
            prob += d[i] >= 0
            prob += self_used[i] >= 0
            self_used[i].lowBound = 0
            prob += grid_sell[i] >= 0
            # Binary constraints for conditional grid sell
            prob += grid_sell[i] <= self.MAX_DISCHARGE * grid_sell_binary[i]
            prob += grid_sell[i] >= self.MIN_GRID_SELL_AMOUNT * grid_sell_binary[i]
            prob += epsilon * grid_sell_binary[i] <= grid_sell[i] + epsilon * (1 - grid_sell_binary[i])
            # Prohibit selling to grid if the price is below the minimum price
            if self.prices[i] < self.MIN_GRID_SELL_PRICE:
                prob += grid_sell_binary[i] == 0
            # Prevent simultaneous charging and discharging
            prob += grid_buy[i] <= self.MAX_CHARGE * (1 - u[i])
            prob += solar_to_battery[i] <= self.MAX_CHARGE * (1 - u[i])
            if not allow_grid_charging:
                prob += grid_buy[i] == 0
            # Discharge split between self-use and grid sell
            prob += d[i] <= self.MAX_DISCHARGE * u[i]
            prob += d[i] == self_used[i] + grid_sell[i]
            # Limit self-consumption to actual household needs
            prob += self_used[i] + solar_self_used[i] <= self.self_consumption

    def _set_objective(self, prob, vars):
        # Define objective function to maximize net energy value
        terms = []
        for i in range(self.current_time, self.n):
            terms.append((self.prices[i] - self.GRID_SELL_COST) * vars['grid_sell'][i]) # profit from selling to grid
            terms.append(-(self.prices[i] + self.GRID_BUY_COST + self.GRID_FEE) * vars['grid_buy'][i]) # cost of buying from grid
            terms.append((self.GRID_FEE + self.prices[i] + self.GRID_BUY_COST) * vars['solar_self_used'][i]) # self-consumption savings from solar
            terms.append((self.GRID_FEE * vars['self_used'][i] * self.BATTERY_GRID_SHARE_PERCENT) + 
                (self.prices[i] + self.GRID_BUY_COST) * vars['self_used'][i]) # self-consumption savings from battery
        # Set target function
        prob += lpSum(terms)

    def _extract_results(self, prob, vars):
        # Extract optimization results from solved problem
        d = vars['d']
        soc = vars['soc']
        self_used = vars['self_used']
        solar_self_used = vars['solar_self_used']
        solar_to_battery = vars['solar_to_battery']
        solar_to_grid = vars['solar_to_grid']
        grid_buy = vars['grid_buy']
        grid_sell = vars['grid_sell']

        # Calculate energy profit from grid selling and buying
        energy_profit = sum((self.prices[i] - self.GRID_SELL_COST) * grid_sell[i].varValue - (self.prices[i] + self.GRID_BUY_COST + self.GRID_FEE) * grid_buy[i].varValue for i in range(self.current_time, self.n))
        # Combined grid fee savings and alternative cost savings
        self_consumption_savings = sum((self.GRID_FEE + self.prices[i] + self.GRID_BUY_COST) * (self_used[i].varValue + solar_self_used[i].varValue) for i in range(self.current_time, self.n))
        # Total profit
        total_profit_value = energy_profit + self_consumption_savings

        # Create result arrays with zeros for hours before current_time
        result_grid_buy = [0] * self.n
        result_grid_sell = [0] * self.n
        result_self_used = [0] * self.n
        result_solar_self_used = [0] * self.n
        result_solar_to_battery = [0] * self.n
        result_solar_to_grid = [0] * self.n
        result_discharge = [0] * self.n
        result_soc = [self.initial_soc] * self.n
        result_inverter_mode = [""] * self.n

        # Fill values for hours from current_time to n
        for i in range(self.current_time, self.n):
            result_grid_buy[i] = grid_buy[i].varValue
            result_grid_sell[i] = grid_sell[i].varValue
            result_self_used[i] = max(0, self_used[i].varValue)
            result_solar_self_used[i] = solar_self_used[i].varValue
            result_solar_to_battery[i] = solar_to_battery[i].varValue
            result_solar_to_grid[i] = solar_to_grid[i].varValue
            result_discharge[i] = d[i].varValue
            result_soc[i] = soc[i].varValue

            # Set inverter_mode according to rules
            # Rule 1: If we're buying significant amount of energy from the grid, set to TOU charge mode
            # This mode prioritizes charging the battery from the grid during low-price periods
            if grid_buy[i].varValue >= self.MIN_CYCLE_AMOUNT:
                result_inverter_mode[i] = 'TOU charge'
            # Rule 2: If we're selling significant amount of energy to the grid and selling more than storing in battery
            # This mode prioritizes feeding excess energy to the grid when prices are favorable
            elif (grid_sell[i].varValue >= self.MIN_CYCLE_AMOUNT and grid_sell[i].varValue > solar_to_battery[i].varValue):
                result_inverter_mode[i] = 'feed to grid'
            # Rule 3: If no energy is being used from the battery and solar production is less than consumption
            # This mode indicates no battery activity is needed during periods of low solar production
            elif (self_used[i].varValue == 0 and 
                solar_self_used[i].varValue < self.self_consumption and 
                self.pv_forecast[i] < self.self_consumption):
                result_inverter_mode[i] = 'TOU none'
            # Rule 4: Default case - maximize self-consumption by using available battery and solar resources
            # This mode prioritizes using stored energy and current solar production for self-consumption
            else:
                result_inverter_mode[i] = 'max self consumption'

        return {
            'status': LpStatus[prob.status],
            'profit': total_profit_value,
            'grid_buy': result_grid_buy,
            'grid_sell': result_grid_sell,
            'self_used': result_self_used,
            'solar_self_used': result_solar_self_used,
            'solar_to_battery': result_solar_to_battery,
            'solar_to_grid': result_solar_to_grid,
            'discharge': result_discharge,
            'soc': result_soc,
            'inverter_mode': result_inverter_mode
        }
