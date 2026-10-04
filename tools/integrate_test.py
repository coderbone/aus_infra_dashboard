#!/usr/bin/env python3

import time

NOW = int(time.time())

MINUTE_SIZE = 60
HOUR_SIZE = MINUTE_SIZE * 60
DAY_SIZE = 24 * HOUR_SIZE

power_history = [(NOW - DAY_SIZE * 1, 200.0)]  # yesterday it had 200 Mwh stored

charge_history = [(NOW - HOUR_SIZE * 24, 20.0),
                  (NOW - HOUR_SIZE * 23, 20.0),
                  (NOW - HOUR_SIZE * 22, 10.0),
                  (NOW - HOUR_SIZE * 21, 10.0),
                  (NOW - HOUR_SIZE * 20,  0.0),
                  (NOW - HOUR_SIZE * 19,  0.0),
                  (NOW - HOUR_SIZE * 18,  0.0),
                  (NOW - HOUR_SIZE * 17,  0.0),
                  (NOW - HOUR_SIZE * 16,  0.0),
                  (NOW - HOUR_SIZE * 15,  0.0),
                  (NOW - HOUR_SIZE * 14, -5.0),
                  (NOW - HOUR_SIZE * 13, -10.0),
                  (NOW - HOUR_SIZE * 12, -15.0),
                  (NOW - HOUR_SIZE * 11, -20.0),
                  (NOW - HOUR_SIZE * 10, -20.0),
                  (NOW - HOUR_SIZE *  9, -20.0),
                  (NOW - HOUR_SIZE *  8, -20.0),
                  (NOW - HOUR_SIZE *  7, -10.0),
                  (NOW - HOUR_SIZE *  6, -5.0),
                  (NOW - HOUR_SIZE *  5,  0.0),
                  (NOW - HOUR_SIZE *  4, 10.0),
                  (NOW - HOUR_SIZE *  3, 20.0),
                  (NOW - HOUR_SIZE *  2, 20.0),
                  (NOW - HOUR_SIZE *  1, 20.0),
                  (NOW - HOUR_SIZE *  0, 20.0),
                  ]

def _get_prev_value(when, series):
    best = (0, 0)
    best_delta = DAY_SIZE * 100
    for ts, pw in series:
        if ts > when:
            continue
        d = when - ts
        if d < best_delta:
            best_delta = d
            best = (ts, pw)
    return best

def _get_prev_power_value(when):
    return _get_prev_value(when, power_history)

def _get_prev_charge_value(when):
    return _get_prev_value(when, charge_history)

def infer_power(when):
    pw_ts, pw = _get_prev_power_value(when)

    # integrate charge/discharge
    net_charge = 0.0
    dt = 5 * MINUTE_SIZE
    for t in range(pw_ts, when, dt):
        ch_ts, ch = _get_prev_charge_value(t)
        ch *= dt / HOUR_SIZE  # convert charge rate to storage amount
        if ch > 0:
            net_charge += ch * 0.9  # efficiency factor
        else:
            net_charge += ch
    return pw + net_charge

def make_inferred_series():
    inferred = []
    for t in range(NOW - DAY_SIZE, NOW, 5 * MINUTE_SIZE):
        inferred.append((t, infer_power(t)))
    return inferred

if __name__ == '__main__':
    inferred = make_inferred_series()
    # write results in format gnuplot can work with
    # gnuplot -e "set term png; plot 'foo.txt' using 2,1" > foo.png
    for ts, pw in inferred:
        print(str(ts) + ', ' + str(pw))
