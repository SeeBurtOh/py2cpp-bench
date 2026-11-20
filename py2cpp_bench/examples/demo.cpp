// Handwritten serial translation for offline pipeline testing. Not a model result.
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    const auto iterations = argc > 1 ? std::stoull(argv[1]) : 200000000ULL;
    const auto param1 = argc > 2 ? std::stoull(argv[2]) : 4ULL;
    const auto param2 = argc > 3 ? std::stoull(argv[3]) : 1ULL;
    if (argc > 4 || param1 < 2 || param2 >= param1) return 2;
    const auto start = std::chrono::steady_clock::now();
    double result = 1.0;
    for (std::uint64_t i = 1; i <= iterations; ++i) {
        auto j = i * param1 - param2;
        result -= 1.0 / static_cast<double>(j);
        j = i * param1 + param2;
        result += 1.0 / static_cast<double>(j);
    }
    result *= 4;
    const auto end = std::chrono::steady_clock::now();
    std::cout << "Result: " << std::fixed << std::setprecision(12) << result << '\n';
    std::cout << "Execution Time: " << std::setprecision(6)
              << std::chrono::duration<double>(end - start).count() << " seconds\n";
}
