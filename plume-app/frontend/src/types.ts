export type StabilityClass = 'A' | 'B' | 'C' | 'D' | 'E' | 'F'

export interface SourceInput {
  name: string
  lon: number
  lat: number
  stack_height_m: number
  emission_rate_g_s: number
  stack_diameter_m: number
  exit_velocity_ms: number
  stack_temp_k: number
  pollutant: string
}

export interface MeteorologyInput {
  name?: string
  wind_from_deg: number
  wind_speed_ms: number
  stability_class: StabilityClass
  ambient_temp_k: number
  pressure_hpa: number
  background_conc_ug_m3: number
}

export interface GridSpec {
  downwind_extent_m: number
  crosswind_extent_m: number
  upwind_extent_m: number
  nx: number
  ny: number
}

export interface SourceRow extends SourceInput {
  id: number
}

export interface MetRow extends MeteorologyInput {
  id: number
  name: string
}

export interface PlumeGridResponse {
  source_lonlat: [number, number]
  crs_note: string
  grid: {
    nx: number
    ny: number
    x_edges_m: number[]
    y_edges_m: number[]
    lon_grid: number[][]
    lat_grid: number[][]
    spacing_downwind_m: number
    spacing_crosswind_m: number
    corners_lonlat: [number, number][]
    sampling_extent_lonlat: Record<string, number>
    flat_earth_warning: boolean
    resolution_disclaimer: string
  }
  plume_field_ug_m3: number[][]
  background_conc_ug_m3: number
  total_conc_ug_m3: number[][]
  iso_levels_ug_m3: number[]
  effective_stack_height_m: number
  plume_rise_delta_h_m: number
  wind: {
    wind_from_deg: number
    transport_bearing_deg: number
    downwind_unit_E_N: [number, number]
    crosswind_unit_E_N: [number, number]
    dot_product_check: number
    norm_check: number
    interpretation: string
    wind_speed_ms: number
    stability_class: string
  }
  source_term: Record<string, any>
  diagnostics: Record<string, any>
  validity: Record<string, any>
  disclaimer: string
}

export interface PlumeGridRequest {
  source: SourceInput
  meteorology: MeteorologyInput
  grid: GridSpec
  plume_rise: { use_plume_rise: boolean }
  source_override?: Record<string, number | null>
  met_override?: Record<string, number | string | null>
  parameterization: 'briggs_rural' | 'power_law'
  power_law?: { ay: number; py: number; az: number; pz: number } | null
  calm_threshold_ms: number
}

export interface CheckResult {
  id: string
  title: string
  description: string
  passed: boolean
  expected: string
  actual: string
  tolerance?: number
  extra?: Record<string, any>
}

export interface ChecksReport {
  title: string
  all_passed: boolean
  n_passed: number
  n_total: number
  results: CheckResult[]
  note: string
}

export interface ApiError {
  error: string
  message: string
  action?: string
}

/* ---------- 稳定度扫描（独立试算） ---------- */

export interface StabilitySweepRequest {
  source: SourceInput
  meteorology: MeteorologyInput
  receptor_distances_m: number[]
  stability_classes?: StabilityClass[] | null
  plume_rise: { use_plume_rise: boolean }
  source_override?: Record<string, number | null>
  met_override?: Record<string, number | string | null>
  parameterization: 'briggs_rural' | 'power_law'
  power_law?: { ay: number; py: number; az: number; pz: number } | null
  calm_threshold_ms: number
}

export interface SweepReceptor {
  distance_m: number
  lonlat: [number, number]
  east_north_m: [number, number]
}

export interface SweepPoint {
  distance_m: number
  plume_conc_ug_m3: number
  background_conc_ug_m3: number
  total_conc_ug_m3: number
  sigma_y_m: number
  sigma_z_m: number
  within_valid_range: boolean | null
  range_flag: string
}

export interface SweepStabilityResult {
  stability_class: StabilityClass
  stability_cn: string
  points: SweepPoint[]
  peak_on_receptors: {
    distance_m: number
    plume_conc_ug_m3: number
    total_conc_ug_m3: number
    sampled_on_receptors: boolean
    note: string
  }
}

export interface StabilitySweepResponse {
  source_lonlat: [number, number]
  wind: {
    wind_from_deg: number
    transport_bearing_deg: number
    interpretation: string
    wind_speed_ms: number
  }
  receptors: SweepReceptor[]
  stability_classes: StabilityClass[]
  background_conc_ug_m3: number
  effective_stack_height_m: number
  plume_rise_delta_h_m: number
  results: SweepStabilityResult[]
  units: Record<string, string>
  coefficients: {
    parameterization: string
    per_stability?: {
      stability: string
      stability_cn: string
      sigma_y: string
      sigma_z_form: string
      sigma_z_constants: Record<string, number>
      x_valid_range_m: [number, number]
    }[]
    formula?: string
    coeffs?: { ay: number; py: number; az: number; pz: number }
  }
  validity: {
    briggs_valid_range_m: [number, number] | null
    out_of_range_policy: string
    peak_note: string
    evaluation_path: string
  }
  trial_note: string
  disclaimer: string
}
