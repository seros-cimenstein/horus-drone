#ifndef HORUS_COMMANDER_HH_
#define HORUS_COMMANDER_HH_

#include <QString>
#include <QStringList>

#include <gz/gui/qt.h>
#include <gz/gui/Plugin.hh>
#include <gz/msgs/stringmsg.pb.h>
#include <gz/transport/Node.hh>

class HorusCommander : public gz::gui::Plugin
{
  Q_OBJECT
  Q_PROPERTY(QString eventLog READ EventLog NOTIFY EventLogChanged)
  Q_PROPERTY(QString status READ Status NOTIFY StatusChanged)

  public: HorusCommander();
  public: ~HorusCommander() override;

  /// \brief Publish a task string to the HORUS commander topic.
  /// \return True when a non-empty task was published successfully.
  public: Q_INVOKABLE bool SendTask(const QString &_text);

  /// \brief Publish one emergency action to the authenticated host bridge.
  public: Q_INVOKABLE bool SendEmergency(const QString &_action);

  /// \brief Start a fresh commander mission.
  public: Q_INVOKABLE bool ResetMission();

  /// \brief Get the event log for display in QML.
  public: QString EventLog() const;

  /// \brief Get the latest commander state for display in QML.
  public: QString Status() const;

  signals: void EventLogChanged();
  signals: void StatusChanged();
  signals: void EventMessage(const QString &_message);
  signals: void StateMessage(const QString &_message);

  private: void OnEventMsg(const gz::msgs::StringMsg &_msg);
  private: void OnStateMsg(const gz::msgs::StringMsg &_msg);
  private: void AppendEvent(const QString &_message);
  private: void UpdateStatus(const QString &_status);

  private: gz::transport::Node node_;
  private: gz::transport::Node::Publisher taskPublisher_;
  private: gz::transport::Node::Publisher emergencyPublisher_;
  private: gz::transport::Node::Publisher resetPublisher_;
  private: QStringList eventLines_;
  private: QString eventLog_{QStringLiteral("Waiting for commander events...")};
  private: QString status_{QStringLiteral("Waiting for commander state...")};
};

#endif  // HORUS_COMMANDER_HH_
