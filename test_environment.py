import platform

import numpy as np
import pandas as pd
import scipy
import simpy


def main() -> None:
    print("Python:", platform.python_version())
    print("NumPy:", np.__version__)
    print("pandas:", pd.__version__)
    print("SciPy:", scipy.__version__)
    print("SimPy:", simpy.__version__)
    print("test Git ...")

    probabilities = np.array([0.5, 0.3, 0.2])
    entropy = -np.sum(probabilities * np.log2(probabilities))

    print(f"Shannon entropy: {entropy:.6f}")


if __name__ == "__main__":
    main()