pub mod wasm_analysis;
use anyhow::{anyhow, bail, Result};
use scoregate_score::CONFIG_DRIFT_MANIFEST_FIELDS;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeSet;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DriftDiffEntry {
    pub field: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observed: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DriftReport {
    pub status: String,
    pub diffs: Vec<DriftDiffEntry>,
}

fn known_fields() -> BTreeSet<&'static str> {
    CONFIG_DRIFT_MANIFEST_FIELDS.iter().copied().collect()
}

pub fn compare_config_manifests(approved: &Value, observed: &Value) -> Result<DriftReport> {
    let approved =
        approved.as_object().ok_or_else(|| anyhow!("approved manifest must be a JSON object"))?;
    let observed =
        observed.as_object().ok_or_else(|| anyhow!("observed manifest must be a JSON object"))?;

    let known = known_fields();
    let mut diffs = Vec::new();

    for field in approved.keys() {
        if !known.contains(field.as_str()) {
            diffs.push(DriftDiffEntry {
                field: field.clone(),
                status: "unknown_approved_field".into(),
                expected: approved.get(field).cloned(),
                observed: observed.get(field).cloned(),
            });
        }
    }

    for field in observed.keys() {
        if !known.contains(field.as_str()) {
            diffs.push(DriftDiffEntry {
                field: field.clone(),
                status: "unknown_observed_field".into(),
                expected: approved.get(field).cloned(),
                observed: observed.get(field).cloned(),
            });
        }
    }

    for field in CONFIG_DRIFT_MANIFEST_FIELDS {
        let expected = approved.get(*field);
        let actual = observed.get(*field);

        match (expected, actual) {
            (Some(left), Some(right)) if left != right => diffs.push(DriftDiffEntry {
                field: (*field).into(),
                status: "drift".into(),
                expected: Some(left.clone()),
                observed: Some(right.clone()),
            }),
            (Some(left), None) => diffs.push(DriftDiffEntry {
                field: (*field).into(),
                status: "missing_observed_field".into(),
                expected: Some(left.clone()),
                observed: None,
            }),
            (None, Some(right)) => diffs.push(DriftDiffEntry {
                field: (*field).into(),
                status: "unexpected_observed_field".into(),
                expected: None,
                observed: Some(right.clone()),
            }),
            _ => {}
        }
    }

    diffs.sort_by(|left, right| left.field.cmp(&right.field).then(left.status.cmp(&right.status)));

    Ok(DriftReport { status: if diffs.is_empty() { "ok".into() } else { "drifted".into() }, diffs })
}

pub fn parse_manifest_json(raw: &str) -> Result<Value> {
    let value: Value = serde_json::from_str(raw)?;
    if !value.is_object() {
        bail!("manifest must be a JSON object");
    }
    Ok(value)
}

pub fn recommended_manifest_template() -> Value {
    let mut map = Map::new();
    for field in CONFIG_DRIFT_MANIFEST_FIELDS {
        map.insert((*field).into(), Value::Null);
    }
    Value::Object(map)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn build_manifest(entries: &[(&str, Value)]) -> Value {
        let mut map = Map::new();
        for (key, value) in entries {
            map.insert((*key).into(), value.clone());
        }
        Value::Object(map)
    }

    #[test]
    fn equivalent_maps_in_different_orders_produce_identical_reports() {
        let approved_a = build_manifest(&[
            ("alpha", json!(1)),
            ("beta", json!(2)),
            ("gamma", json!(3)),
            ("delta", json!(4)),
        ]);
        let approved_b = build_manifest(&[
            ("delta", json!(4)),
            ("gamma", json!(3)),
            ("beta", json!(2)),
            ("alpha", json!(1)),
        ]);

        let observed_a = build_manifest(&[
            ("alpha", json!(10)),
            ("beta", json!(20)),
            ("gamma", json!(30)),
            ("delta", json!(40)),
        ]);
        let observed_b = build_manifest(&[
            ("gamma", json!(30)),
            ("alpha", json!(10)),
            ("delta", json!(40)),
            ("beta", json!(20)),
        ]);

        let report_a = compare_config_manifests(&approved_a, &observed_a).unwrap();
        let report_b = compare_config_manifests(&approved_b, &observed_b).unwrap();

        let serialized_a = serde_json::to_string(&report_a).unwrap();
        let serialized_b = serde_json::to_string(&report_b).unwrap();

        assert_eq!(serialized_a, serialized_b);
        assert_eq!(report_a, report_b);
    }

    #[test]
    fn differing_states_still_report_expected_differences() {
        let approved = build_manifest(&[("alpha", json!(1)), ("beta", json!(2))]);
        let observed = build_manifest(&[("alpha", json!(1)), ("beta", json!(99))]);

        let report = compare_config_manifests(&approved, &observed).unwrap();

        assert_eq!(report.status, "drifted");
        assert_eq!(report.diffs.len(), 1);
        assert_eq!(report.diffs[0].field, "beta");
        assert_eq!(report.diffs[0].status, "drift");
        assert_eq!(report.diffs[0].expected, Some(json!(2)));
        assert_eq!(report.diffs[0].observed, Some(json!(99)));
    }
}
