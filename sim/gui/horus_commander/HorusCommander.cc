#include "HorusCommander.hh"
#include <QJsonDocument>
#include <QJsonObject>

#include <gz/common/Console.hh>
#include <gz/plugin/Register.hh>

/////////////////////////////////////////////////
HorusCommander::HorusCommander()
  : gz::gui::Plugin()
{
  this->title = "Horus Commander";

  QObject::connect(this, &HorusCommander::EventMessage,
      this, &HorusCommander::AppendEvent, Qt::QueuedConnection);
  QObject::connect(this, &HorusCommander::StateMessage,
      this, &HorusCommander::UpdateStatus, Qt::QueuedConnection);

  this->taskPublisher_ = this->node_.Advertise<gz::msgs::StringMsg>(
      "/horus/task");
  if (!this->taskPublisher_)
    gzerr << "Unable to publish HORUS tasks on [/horus/task]." << std::endl;

  if (!this->node_.Subscribe("/horus/events",
      &HorusCommander::OnEventMsg, this))
  {
    gzerr << "Unable to subscribe to HORUS events on [/horus/events]."
          << std::endl;
  }

  if (!this->node_.Subscribe("/horus/state",
      &HorusCommander::OnStateMsg, this))
  {
    gzerr << "Unable to subscribe to HORUS state on [/horus/state]."
          << std::endl;
  }
}

/////////////////////////////////////////////////
HorusCommander::~HorusCommander()
{
  this->node_.Unsubscribe("/horus/events");
  this->node_.Unsubscribe("/horus/state");
}

/////////////////////////////////////////////////
bool HorusCommander::SendTask(const QString &_text)
{
  const auto task = _text.trimmed();
  if (task.isEmpty() || !this->taskPublisher_)
    return false;

  gz::msgs::StringMsg msg;
  msg.set_data(_text.toStdString());
  return this->taskPublisher_.Publish(msg);
}

/////////////////////////////////////////////////
QString HorusCommander::EventLog() const
{
  return this->eventLog_;
}

/////////////////////////////////////////////////
QString HorusCommander::Status() const
{
  return this->status_;
}

/////////////////////////////////////////////////
void HorusCommander::OnEventMsg(const gz::msgs::StringMsg &_msg)
{
  emit EventMessage(QString::fromStdString(_msg.data()));
}

/////////////////////////////////////////////////
void HorusCommander::OnStateMsg(const gz::msgs::StringMsg &_msg)
{
  emit StateMessage(QString::fromStdString(_msg.data()));
}

/////////////////////////////////////////////////
void HorusCommander::AppendEvent(const QString &_message)
{
  this->eventLines_.append(_message);

  constexpr int kMaxEventLines = 200;
  while (this->eventLines_.size() > kMaxEventLines)
    this->eventLines_.removeFirst();

  this->eventLog_ = this->eventLines_.join(QLatin1Char('\n'));
  emit EventLogChanged();
}

/////////////////////////////////////////////////
void HorusCommander::UpdateStatus(const QString &_status)
{
  QJsonParseError parseError;
  const auto document = QJsonDocument::fromJson(_status.toUtf8(), &parseError);
  QString nextStatus = _status;
  if (parseError.error == QJsonParseError::NoError && document.isObject())
  {
    const auto root = document.object();
    QStringList details;
    const auto state = root.value("state").toString();
    if (!state.isEmpty())
      details.append(state);

    const auto skill = root.value("active_skill").toString();
    const auto taskId = root.value("active_task_id").toString();
    if (!skill.isEmpty())
      details.append(taskId.isEmpty() ? skill : QString("%1 (%2)").arg(skill, taskId));

    const auto appendAge = [&details](const QString &_label, const QJsonValue &_value)
    {
      if (_value.isDouble())
        details.append(QString("%1 %2s").arg(_label).arg(_value.toDouble(), 0, 'f', 1));
    };
    appendAge("observation", root.value("observation_age_s"));
    const auto vehicle = root.value("vehicle").toObject();
    appendAge("heartbeat", vehicle.value("heartbeat_age_s"));
    const auto camera = root.value("perception").toObject().value("camera").toString();
    if (!camera.isEmpty())
      details.append(QString("camera %1").arg(camera));
    if (!details.isEmpty())
      nextStatus = details.join(QStringLiteral(" | "));
  }

  if (this->status_ == nextStatus)
    return;

  this->status_ = nextStatus;
  emit StatusChanged();
}

// Register this plugin
GZ_ADD_PLUGIN(HorusCommander, gz::gui::Plugin)
