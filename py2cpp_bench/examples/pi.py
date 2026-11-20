"""Original pi loop, with runtime inputs: iterations [param1 [param2]]."""

import sys
import time


def calculate(iterations, param1, param2):
    result = 1.0
    for i in range(1, iterations + 1):
        j = i * param1 - param2
        result -= (1 / j)
        j = i * param1 + param2
        result += (1 / j)
    return result


if __name__ == "__main__":
    if len(sys.argv) > 4:
        raise SystemExit("usage: pi.py [iterations [param1 [param2]]]")
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000_000
    param1 = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    param2 = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if iterations < 0 or param1 < 2 or not 0 <= param2 < param1:
        raise SystemExit("require iterations >= 0, param1 >= 2, 0 <= param2 < param1")

    start_time = time.time()
    result = calculate(iterations, param1, param2) * 4
    end_time = time.time()

    print(f"Result: {result:.12f}")
    print(f"Execution Time: {(end_time - start_time):.6f} seconds")
