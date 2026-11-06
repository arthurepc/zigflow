/*
 * Copyright 2025 - 2026 Zigflow authors <https://github.com/zigflow/zigflow/graphs/contributors>
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package tasks

import (
	"testing"

	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
)

type registrationTestWorker struct {
	worker.Worker
	registrations int
}

func (w *registrationTestWorker) RegisterActivityWithOptions(_ any, _ activity.RegisterOptions) {
	w.registrations++
}

func registrationsForWorker(w worker.Worker) int {
	count := 0
	activityRegistry.Range(func(key, _ any) bool {
		if key.(activityRegistryKey).worker == w {
			count++
		}
		return true
	})
	return count
}

func TestForgetWorkerActivitiesReleasesRegistrations(t *testing.T) {
	w := &registrationTestWorker{}
	registerActivityOnce(w, func() {}, "workflow.step")
	registerActivityOnce(w, func() {}, "workflow.step")
	require.Equal(t, 1, w.registrations, "duplicate activity registration must still be skipped")
	require.Equal(t, 1, registrationsForWorker(w))

	ForgetWorkerActivities(w)
	require.Zero(t, registrationsForWorker(w), "stopped workers must not remain in the registry")
}

func TestForgetWorkerActivitiesPreservesActiveWorker(t *testing.T) {
	stopped := &registrationTestWorker{}
	active := &registrationTestWorker{}
	registerActivityOnce(stopped, func() {}, "workflow.step")
	registerActivityOnce(active, func() {}, "workflow.step")
	t.Cleanup(func() { ForgetWorkerActivities(active) })

	ForgetWorkerActivities(stopped)
	require.Zero(t, registrationsForWorker(stopped))
	require.Equal(t, 1, registrationsForWorker(active))
}
