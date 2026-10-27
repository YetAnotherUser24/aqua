% sysid_and_tune.m
% Peristaltic Pump: System Identification (ARX & FOPDT) and PI Tuning via pidtune
clear; clc;
try
    close all force;
catch
end
try
    set(groot, 'DefaultFigureVisible', 'off');
catch
end

%% 1. Load Data
disp('Loading step_response.csv...');
csv_file = 'data/step_response.csv';
if ~isfile(csv_file) && isfile('../scripts/data/step_response.csv')
    csv_file = '../scripts/data/step_response.csv';
end

try
    data = readtable(csv_file);
catch
    error('Could not find step_response.csv. Did the Python logger run successfully?');
end

t = data.Time_s;
t = t - t(1);
Ts = mean(diff(t));
disp(['Average Sample Time: ', num2str(Ts, '%.4f'), ' seconds (', num2str(1/Ts, '%.1f'), ' Hz)']);

% Duty input: canonical SI range is [0.0, 1.0]
if any(strcmp(data.Properties.VariableNames, 'Target_Duty'))
    u = data.Target_Duty;
elseif any(strcmp(data.Properties.VariableNames, 'Compensated_Duty'))
    u = data.Compensated_Duty;
else
    u = data{:, 2};
end

if max(u) > 1.5
    disp('Normalizing duty from [0..100%] to [0..1.0] fractional SI units...');
    u = u / 100.0;
end

% Velocity output: canonical SI unit is rad/s
if any(strcmp(data.Properties.VariableNames, 'Rad_s'))
    y = data.Rad_s;
elseif any(strcmp(data.Properties.VariableNames, 'RPM'))
    y = data.RPM * (pi / 30);
else
    y = data{:, 4};
end

%% 2. System Identification (ARX & Continuous FOPDT)
disp('Performing System Identification...');
data_id = iddata(y, u, Ts, 'Name', 'Peristaltic Pump Telemetry');
data_id.InputName = 'Duty'; data_id.OutputName = 'AngularVelocity';
data_id.InputUnit = 'duty [0..1]'; data_id.OutputUnit = 'rad/s';
data_id.TimeUnit = 's';

% Discrete ARX Model (2 poles, 1 zero, 1 sample delay)
% A(q)*y(t) = B(q)*u(t-nk) + e(t)
disp('Estimating discrete ARX([2, 1, 1]) model...');
sys_arx = arx(data_id, [2 1 1]);
disp(sys_arx);

% Continuous FOPDT Model: G(s) = K / (tau*s + 1) * exp(-theta*s)
disp('Estimating continuous FOPDT model (tfest)...');
has_tfest = false;
try
    sys_tf = tfest(data_id, 1, 0, NaN);
    P = tf(sys_tf.Numerator, sys_tf.Denominator, 'InputDelay', sys_tf.IODelay);
    has_tfest = true;
    disp('Identified FOPDT Model:');
    disp(P);
catch ME
    disp('tfest failed or Toolbox missing. Converting discrete ARX to continuous...');
    P_discrete = tf(sys_arx);
    P = d2c(P_discrete);
    sys_tf = P;
    disp('Converted ARX Model to Continuous Plant:');
    disp(P);
end

%% 3. Controller Tuning (PI Reference Tracking)
disp('Calculating Optimal PI Gains via MATLAB pidtune...');
% Per hardware safety invariants:
% Peristaltic pumps use PURE PI control. Derivative (Kd) is strictly 0
% to prevent amplification of AS5600 discrete magnetic ticks.
try
    opts = pidtuneOptions('DesignFocus', 'reference-tracking');
    [C, info] = pidtune(P, 'PI', opts);
    
    % Ensure pure PI
    Kp = C.Kp;
    Ki = C.Ki;
    Kd = 0.0;
    
    disp('==================================================');
    disp('OPTIMAL PI GAINS (MATLAB pidtune for ESP32-S3):');
    disp(['  Kp = ', num2str(Kp, '%.6f'), ' [duty / (rad/s)]']);
    disp(['  Ki = ', num2str(Ki, '%.6f'), ' [duty / (rad/s * s)]']);
    disp(['  Kd = ', num2str(Kd, '%.6f')]);
    disp('==================================================');
    
    % Ensure target directories exist
    if ~exist('results/models', 'dir'), mkdir('results/models'); end
    if ~exist('results/figures/pidtune', 'dir'), mkdir('results/figures/pidtune'); end
    
    % Save gains in format compatible with test_gains.py & pipeline.py
    fid = fopen('results/models/pidtune_gains.txt', 'w');
    fprintf(fid, '--kp %.6f --ki %.6f --kd 0.000000\n', Kp, Ki);
    fclose(fid);
    disp('Saved gains to results/models/pidtune_gains.txt');
    
    %% 4. Generate Figures
    disp('Saving publication figures...');
    
    % 1. Raw Data Input/Output
    f1 = figure('Visible', 'off', 'Position', [100 100 800 600]);
    subplot(2, 1, 1);
    plot(t, u * 100.0, 'b', 'LineWidth', 1.5);
    title('Input: Motor Commanded Duty Cycle (%)', 'FontWeight', 'bold');
    ylabel('Duty (%)'); grid on;
    
    subplot(2, 1, 2);
    plot(t, y, 'r', 'LineWidth', 1.5);
    title('Output: Peristaltic Pump Velocity (rad/s) [AS5600]', 'FontWeight', 'bold');
    xlabel('Time (s)'); ylabel('Velocity (rad/s)'); grid on;
    saveas(f1, 'results/figures/pidtune/sysid_raw_data.png');
    
    % 2. Model Fit Plot (ARX discrete and FOPDT continuous vs Real Data)
    f2 = figure('Visible', 'off', 'Position', [100 100 850 550]);
    [y_sim_fopdt, t_sim] = lsim(sys_tf, u, t);
    opt = simOptions('InitialCondition', zeros(2,1));
    y_sim_arx = sim(sys_arx, u);
    
    plot(t, y, 'k', 'LineWidth', 1.5); hold on;
    plot(t, y_sim_fopdt, 'b--', 'LineWidth', 1.8);
    plot(t, y_sim_arx, 'g:', 'LineWidth', 1.8);
    title('Model Fit vs Experimental Pump Data (AS5600)', 'FontWeight', 'bold');
    xlabel('Time (s)'); ylabel('Velocity (rad/s)');
    legend('Experimental Data', 'FOPDT Model Prediction', 'ARX(2,1,1) Discrete Prediction', 'Location', 'southeast');
    grid on;
    saveas(f2, 'results/figures/pidtune/sysid_model_fit.png');
    
    % 3. Closed Loop Step Response Simulation
    f3 = figure('Visible', 'off', 'Position', [100 100 800 500]);
    C_pi = pid(Kp, Ki, 0.0);
    T_closed = feedback(C_pi * P, 1);
    step(T_closed, 3.0);
    title('Theoretical Closed-Loop Step Response (100 Hz PI Loop)', 'FontWeight', 'bold');
    ylabel('Normalized Velocity [y / y_{sp}]'); xlabel('Time (s)');
    grid on;
    saveas(f3, 'results/figures/pidtune/sysid_closed_loop.png');
    
    % Save MAT file
    save('results/models/sysid_results_pidtune.mat', 'sys_arx', 'sys_tf', 'C', 'P', 'Kp', 'Ki');
    disp('Saved workspace to results/models/sysid_results_pidtune.mat');
    
catch ME
    disp(['Error during tuning or plotting: ', ME.message]);
end

disp('==================================================');
disp('Script finished successfully.');
disp('Para probar en caliente en el ESP32 via Serial Monitor:');
if exist('Kp', 'var')
    fprintf('  TUNE %.6f %.6f 0.0\n', Kp, Ki);
end
disp('==================================================');
