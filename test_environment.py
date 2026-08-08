import platform

import numpy as np
import pandas as pd
import scipy
import simpy


def main() -> None:
    print("Python 버전:", platform.python_version())
    print("NumPy 버전:", np.__version__)
    print("pandas 버전:", pd.__version__)
    print("SciPy 버전:", scipy.__version__)
    print("SimPy 버전:", simpy.__version__)

    probabilities = np.array([0.5, 0.3, 0.2])
    entropy = -np.sum(probabilities * np.log2(probabilities))

    print(f"Shannon 엔트로피: {entropy:.6f}")


if __name__ == "__main__":
    main()
