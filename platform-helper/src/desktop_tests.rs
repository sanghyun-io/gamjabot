use super::*;

#[test]
fn signed_request_rejects_tampering_and_replay() {
    let key = [7u8; 32];
    let mut state = LeaseState::default();
    let request = test_request("lease_acquire", 1, "a1", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    let signed = sign_for_test(&key, &request);
    assert!(state.dispatch(&key, &signed, &FakeDriver).is_ok());
    assert!(state.dispatch(&key, &signed, &FakeDriver).is_err());
    let mut tampered = signed.clone();
    tampered[20] ^= 1;
    assert!(state.dispatch(&key, &tampered, &FakeDriver).is_err());
}

#[test]
fn other_room_cannot_use_lease() {
    let key = [8u8; 32];
    let mut state = LeaseState::default();
    let acquire = sign_for_test(
        &key,
        &test_request("lease_acquire", 1, "a1", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    );
    state.dispatch(&key, &acquire, &FakeDriver).unwrap();
    let action = sign_for_test(
        &key,
        &test_request("action", 2, "a2", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    );
    assert!(state.dispatch(&key, &action, &FakeDriver).is_err());
}

#[test]
fn lost_release_ack_can_be_retried_only_by_the_same_lease() {
    let key = [10u8; 32];
    let mut state = LeaseState::default();
    let lease = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let acquire = sign_for_test(&key, &test_request("lease_acquire", 1, "a1", lease));
    state.dispatch(&key, &acquire, &FakeDriver).unwrap();
    for _ in 0..2 {
        let release = sign_for_test(&key, &test_request("lease_release", 1, "a1", lease));
        assert_eq!(
            state.dispatch(&key, &release, &FakeDriver).unwrap().state,
            "completed"
        );
    }
    let other = sign_for_test(
        &key,
        &test_request("lease_acquire", 2, "b1", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    );
    state.dispatch(&key, &other, &FakeDriver).unwrap();
    let stale = sign_for_test(&key, &test_request("lease_release", 1, "a1", lease));
    assert!(state.dispatch(&key, &stale, &FakeDriver).is_err());
}

#[test]
fn backend_disconnect_does_not_free_in_flight_native_effect() {
    use std::sync::mpsc;
    use std::time::Duration;
    let directory =
        std::env::temp_dir().join(format!("gamja-native-test-{}", hex(&rand_test_id())));
    std::fs::create_dir(&directory).unwrap();
    let path = directory.join("effect.lock");
    let key = [9u8; 32];
    let lease_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let lease_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    let mut a = LeaseState::with_lock_path(path.clone());
    let acquire_a = sign_for_test(&key, &test_request("lease_acquire", 1, "a1", lease_a));
    assert_eq!(
        a.dispatch(&key, &acquire_a, &FakeDriver).unwrap().state,
        "completed"
    );
    let (entered_tx, entered_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    struct BlockingDriver(mpsc::Sender<()>, mpsc::Receiver<()>);
    impl Driver for BlockingDriver {
        fn execute(&self, _: &Request) -> Response {
            self.0.send(()).unwrap();
            self.1.recv().unwrap();
            Response::ok("synthetic effect finished")
        }
    }
    let action_a = sign_for_test(&key, &test_request("action", 1, "a1", lease_a));
    let worker = std::thread::spawn(move || {
        let result = a
            .dispatch(&key, &action_a, &BlockingDriver(entered_tx, finish_rx))
            .unwrap();
        assert_eq!(result.state, "completed");
        // Simulate backend death: native ownership persists until this call ends.
    });
    entered_rx.recv_timeout(Duration::from_secs(1)).unwrap();
    let mut b = LeaseState::with_lock_path(path.clone());
    let acquire_b = sign_for_test(&key, &test_request("lease_acquire", 2, "b1", lease_b));
    assert_eq!(
        b.dispatch(&key, &acquire_b, &FakeDriver).unwrap().state,
        "failed"
    );
    finish_tx.send(()).unwrap();
    worker.join().unwrap();
    let retry_b = sign_for_test(&key, &test_request("lease_acquire", 2, "b2", lease_b));
    assert_eq!(
        b.dispatch(&key, &retry_b, &FakeDriver).unwrap().state,
        "completed"
    );
    drop(b);
    std::fs::remove_file(path).unwrap();
    std::fs::remove_dir(directory).unwrap();
}

#[test]
fn expired_and_excessive_deadlines_never_execute_or_acquire() {
    let key = [11; 32];
    let mut state = LeaseState::default();
    for deadline in [now_ms() - 1, now_ms() + 30_000] {
        let mut request =
            test_request("lease_acquire", 1, "a1", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        request.deadline_ms = deadline;
        assert!(state
            .dispatch(&key, &sign_for_test(&key, &request), &FakeDriver)
            .is_err());
        assert!(state.owner.is_none());
    }
}
#[test]
fn duplicate_action_with_new_request_id_is_never_replayed() {
    let key = [12; 32];
    let mut state = LeaseState::default();
    let lease = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    state
        .dispatch(
            &key,
            &sign_for_test(&key, &test_request("lease_acquire", 1, "a1", lease)),
            &FakeDriver,
        )
        .unwrap();
    assert_eq!(
        state
            .dispatch(
                &key,
                &sign_for_test(&key, &test_request("action", 1, "a2", lease)),
                &FakeDriver
            )
            .unwrap()
            .state,
        "completed"
    );
    assert!(state
        .dispatch(
            &key,
            &sign_for_test(&key, &test_request("action", 1, "a2", lease)),
            &FakeDriver
        )
        .is_err());
}
#[test]
fn denied_permission_is_typed_and_does_not_replay() {
    struct DeniedDriver;
    impl Driver for DeniedDriver {
        fn execute(&self, _: &Request) -> Response {
            Response::fail("accessibility_denied", "Permission denied.")
        }
    }
    let key = [13; 32];
    let mut state = LeaseState::default();
    let lease = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    state
        .dispatch(
            &key,
            &sign_for_test(&key, &test_request("lease_acquire", 1, "a1", lease)),
            &DeniedDriver,
        )
        .unwrap();
    let response = state
        .dispatch(
            &key,
            &sign_for_test(&key, &test_request("action", 1, "a2", lease)),
            &DeniedDriver,
        )
        .unwrap();
    assert_eq!(response.code, Some("accessibility_denied"));
    assert_eq!(response.state, "failed");
    assert!(state
        .dispatch(
            &key,
            &sign_for_test(&key, &test_request("action", 1, "a2", lease)),
            &DeniedDriver
        )
        .is_err());
}
#[test]
fn malformed_unicode_mac_is_a_refusal_not_a_process_crash() {
    let key = [14; 32];
    let request = test_request("lease_acquire", 1, "a1", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    let input = serde_json::to_vec(&serde_json::json!({"body": STANDARD.encode(serde_json::to_vec(&request).unwrap()), "mac": "€".repeat(21) + "a"})).unwrap();
    assert!(LeaseState::default()
        .dispatch(&key, &input, &FakeDriver)
        .is_err());
}

#[test]
fn owner_permission_control_requires_authentication_but_no_effect_lease() {
    let key = [15; 32];
    let body = serde_json::to_vec(&serde_json::json!({"version":1, "request_id":hex(&rand_test_id()), "deadline_ms":now_ms()+1_000, "operation":"permission_status"})).unwrap();
    let signed = serde_json::to_vec(
        &serde_json::json!({"body":STANDARD.encode(&body),"mac":hex(&mac(&key,&body))}),
    )
    .unwrap();
    let mut state = LeaseState::default();
    let response = state
        .dispatch(&key, &signed, &FakeDriver)
        .expect("authenticated owner status control was denied");
    assert_eq!(response.state, "completed");
    let response = serde_json::to_value(response).unwrap();
    assert_eq!(response["permissions"]["screen_recording"], "unsupported");
    assert_eq!(response["permissions"]["accessibility"], "unsupported");
    assert!(state.owner.is_none());
    assert!(state.dispatch(&key, &signed, &FakeDriver).is_err());
}

#[test]
fn owner_settings_control_accepts_only_fixed_panels_and_no_action_fields() {
    let key = [16; 32];
    let signed = |value: serde_json::Value| {
        let body = serde_json::to_vec(&value).unwrap();
        serde_json::to_vec(
            &serde_json::json!({"body":STANDARD.encode(&body),"mac":hex(&mac(&key,&body))}),
        )
        .unwrap()
    };
    for extra in [
        serde_json::json!({"panel":"arbitrary_url"}),
        serde_json::json!({"panel":"screen_recording","url":"file:///private"}),
        serde_json::json!({"panel":"accessibility","family":"screenshot"}),
    ] {
        let mut value = serde_json::json!({"version":1,"request_id":hex(&rand_test_id()),"deadline_ms":now_ms()+1_000,"operation":"open_settings"});
        value
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        assert!(LeaseState::default()
            .dispatch(&key, &signed(value), &FakeDriver)
            .is_err());
    }
    let value = serde_json::json!({"version":1,"request_id":hex(&rand_test_id()),"deadline_ms":now_ms()+1_000,"operation":"open_settings","panel":"screen_recording"});
    let response = LeaseState::default()
        .dispatch(&key, &signed(value), &FakeDriver)
        .unwrap();
    assert_eq!(response.code, Some("native_settings_unavailable"));
}
#[test]
fn owner_control_retains_version_deadline_and_mac_admission() {
    let key = [17; 32];
    for (version, deadline) in [
        (99, now_ms() + 1_000),
        (1, now_ms() - 1),
        (1, now_ms() + 30_000),
    ] {
        let body = serde_json::to_vec(&serde_json::json!({"version":version,"request_id":hex(&rand_test_id()),"deadline_ms":deadline,"operation":"permission_status"})).unwrap();
        let input = serde_json::to_vec(
            &serde_json::json!({"body":STANDARD.encode(&body),"mac":hex(&mac(&key,&body))}),
        )
        .unwrap();
        assert!(LeaseState::default()
            .dispatch(&key, &input, &FakeDriver)
            .is_err());
    }
}

#[test]
fn final_start_guard_blocks_expired_or_changed_lease_without_driver_effect() {
    use std::cell::Cell;
    struct Counter(Cell<u32>);
    impl Driver for Counter {
        fn execute(&self, _: &Request) -> Response {
            self.0.set(self.0.get() + 1);
            Response::ok("fixture effect")
        }
    }
    let driver = Counter(Cell::new(0));
    let lease = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let mut state = LeaseState::default();
    let mut request = test_request("action", 1, "guard", lease);
    request.family = Some("pointer_click".into());
    request.arguments = Some(serde_json::json!({"x":1,"y":1}));
    request.grant_scope = Some("once".into());
    state.owner = Some((1, request.agent_id, request.workflow_id.clone(), lease.into()));
    request.deadline_ms = now_ms() - 1;
    assert_eq!(state.execute_guarded(&request, &driver).code, Some("review_changed"));
    request.deadline_ms = now_ms() + 10_000;
    state.owner.as_mut().unwrap().0 = 2;
    assert_eq!(state.execute_guarded(&request, &driver).code, Some("review_changed"));
    state.owner.as_mut().unwrap().0 = 1;
    for scope in ["broad", "unknown"] {
        request.grant_scope = Some(scope.into());
        assert_eq!(state.execute_guarded(&request, &driver).code, Some("review_changed"));
    }
    assert_eq!(driver.0.get(), 0);
    request.grant_scope = Some("once".into());
    assert_eq!(state.execute_guarded(&request, &driver).state, "completed");
    assert_eq!(driver.0.get(), 1);
    request.grant_scope = Some("room_session".into());
    assert_eq!(state.execute_guarded(&request, &driver).state, "completed");
    assert_eq!(driver.0.get(), 2);
    request.deadline_ms = now_ms() - 1;
    assert_eq!(state.execute_guarded(&request, &driver).code, Some("review_changed"));
    assert_eq!(driver.0.get(), 2);
}

#[test]
fn final_start_deadline_does_not_reclassify_an_already_started_effect() {
    struct FinishesAfterDeadline;
    impl Driver for FinishesAfterDeadline {
        fn execute(&self, request: &Request) -> Response {
            while now_ms() <= request.deadline_ms {
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            Response::ok("fixture completed after its start deadline")
        }
    }
    let lease = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let mut state = LeaseState::default();
    let mut request = test_request("action", 1, "started", lease);
    state.owner = Some((1, request.agent_id, request.workflow_id.clone(), lease.into()));
    request.deadline_ms = now_ms() + 50;
    assert_eq!(state.execute_guarded(&request, &FinishesAfterDeadline).state, "completed");
}
